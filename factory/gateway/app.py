from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import logfire
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from factory.config import RunName
from factory.db import Db, VersionConflict, append, read_stream
from factory.events import warn
from factory.gateway.body import read_body
from factory.gateway.routes import (
    Decision,
    Ignore,
    Invalid,
    Lookup,
    Start,
    delivery_event,
    delivery_stream,
    recorded_decision,
    route,
)
from factory.gateway.signature import verify_signature
from factory.gateway.state import thread_state

StartRun = Callable[[RunName, dict[str, Any], str], Awaitable[str]]

_ROOT_TEXT = (
    "software-factory gateway is running.\n\n"
    "It has no web UI. GitHub sends webhooks to POST /webhooks/github, and the factory replies on issues and pull "
    "requests.\n"
    "Health checks: GET /health and GET /ready. Setup and usage: docs/quickstart.md in your pydantic-software-factory "
    "repository.\n"
)
_MANIFEST_CODE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def _created_app_text(code: str) -> str:
    return (
        "GitHub App created.\n\n"
        "Exchange this one-time code for the App's credentials within one hour, from your py-software-factory "
        "checkout:\n\n"
        f"  uv run setup-app --exchange-code {code} --credentials-output tmp/github-app.env\n\n"
        "Then follow docs/runbooks/setup.md.\n"
    )


@dataclass(frozen=True)
class GatewayDeps:
    start_run: StartRun
    webhook_secret: str | None
    """None until the GitHub App exists; webhooks are refused until then."""
    db: Db


async def decide(db: Db, event: str, payload: Any, delivery_id: str) -> Decision:
    async def recorded() -> Decision | None:
        events = await read_stream(db, delivery_stream(delivery_id), 0, 1)
        return recorded_decision(events[0]) if events else None

    earlier = await recorded()
    if earlier is not None:
        return earlier
    routed = route(event, payload, delivery_id)
    decision = routed.decide(await thread_state(db, routed.thread)) if isinstance(routed, Lookup) else routed
    try:
        async with db.transaction() as tx:
            appended = await append(tx, delivery_event(delivery_id, event, decision))
    except VersionConflict:
        appended = None
    if appended is None:
        return await recorded() or decision
    return decision


def create_gateway(deps: GatewayDeps) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/", response_class=PlainTextResponse)
    async def root(code: str | None = None) -> str:  # pyright: ignore[reportUnusedFunction]
        # GitHub's App manifest flow redirects here with a one-time code to exchange.
        if code is not None and _MANIFEST_CODE.fullmatch(code):
            return _created_app_text(code)
        return _ROOT_TEXT

    @app.get("/health")
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ready"}

    @app.post("/webhooks/github")
    async def webhook(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if not deps.webhook_secret:
            return JSONResponse({"error": "webhook secret not configured"}, status_code=503)
        event = request.headers.get("x-github-event", "")
        delivery_id = request.headers.get("x-github-delivery", "")

        body = await read_body(request)
        if body is None:
            return JSONResponse({"error": "payload too large"}, status_code=413)
        if not verify_signature(body, request.headers.get("x-hub-signature-256"), deps.webhook_secret):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        if not delivery_id:
            return JSONResponse({"error": "missing x-github-delivery"}, status_code=400)

        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        with logfire.span("GitHub {event} delivery", event=event, delivery_id=delivery_id) as span:
            return await deliver(event, payload, delivery_id, span)

    async def deliver(event: str, payload: Any, delivery_id: str, span: logfire.LogfireSpan) -> JSONResponse:
        try:
            decision = await decide(deps.db, event, payload, delivery_id)
        except Exception as error:
            warn("could not record delivery", delivery_id=delivery_id, event=event, error=str(error))
            return JSONResponse({"error": "could not record delivery"}, status_code=502)
        if isinstance(decision, Ignore):
            span.set_attributes({"decision": "ignore", "reason": decision.reason})
            return JSONResponse({"ignored": decision.reason})
        span.set_attributes({"decision": "start" if isinstance(decision, Start) else "invalid", "task": decision.run})
        if decision.thread:
            span.set_attribute("thread", decision.thread)
        if isinstance(decision, Invalid):
            warn(
                "authenticated delivery produced an invalid run input",
                delivery_id=delivery_id,
                event=event,
                run=decision.run,
                thread=decision.thread,
                reason=decision.reason,
            )
            return JSONResponse({"error": "invalid run input", "run": decision.run}, status_code=422)

        try:
            run_id = await deps.start_run(decision.run, decision.input, decision.idempotency_key)
        except Exception as error:
            warn(
                "could not start run",
                delivery_id=delivery_id,
                event=event,
                run=decision.run,
                thread=decision.thread,
                error=str(error),
            )
            return JSONResponse({"error": "could not start run", "run": decision.run}, status_code=502)
        span.set_attribute("run_id", run_id)
        return JSONResponse({"run_id": run_id, "run": decision.run}, status_code=202)

    return app
