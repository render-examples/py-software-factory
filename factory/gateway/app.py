"""The webhook gateway. It holds no GitHub credential and runs no model.

Every response says what happened to the delivery; GitHub keeps them under the App's Recent Deliveries, so only
failures the gateway can't explain there are logged.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from factory.config import RunName
from factory.db import Db, VersionConflict, append, read_stream
from factory.events import warn
from factory.gateway.body import read_body
from factory.gateway.hub import EventHub
from factory.gateway.routes import (
    Decision,
    Ignore,
    Invalid,
    Lookup,
    delivery_event,
    delivery_stream,
    recorded_decision,
    route,
)
from factory.gateway.signature import verify_signature
from factory.gateway.state import thread_state
from factory.gateway.stream import StreamDeps, stream_router

StartRun = Callable[[RunName, dict[str, Any], str], Awaitable[str]]
"""Starts a task run with an idempotency key, and returns its run ID."""

_ROOT_TEXT = (
    "software-factory gateway is running.\n\n"
    "It has no web UI. GitHub sends webhooks to POST /webhooks/github, and the factory replies on issues and pull "
    "requests.\n"
    "Health checks: GET /health and GET /ready. Setup and usage: docs/quickstart.md in your py-software-factory "
    "repository.\n"
)


@dataclass(frozen=True)
class GatewayDeps:
    start_run: StartRun
    webhook_secret: str
    db: Db
    hub: EventHub
    events_read_token: str


async def decide(db: Db, event: str, payload: Any, delivery_id: str) -> Decision:
    """The delivery's decision, recorded before anything starts. A redelivery replays the recorded decision instead
    of routing again against state that may have moved on."""

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
        # A concurrent redelivery recorded first; the stream only ever holds this delivery's decision.
        appended = None
    if appended is None:
        return await recorded() or decision
    return decision


def create_gateway(deps: GatewayDeps) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(stream_router(StreamDeps(deps.db, deps.hub, deps.events_read_token)))

    # The gateway has no UI; people who open its URL in a browser should see that it's running, not a 404.
    @app.get("/", response_class=PlainTextResponse)
    async def root() -> str:  # pyright: ignore[reportUnusedFunction]
        return _ROOT_TEXT

    @app.get("/health")
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ready"}

    @app.post("/webhooks/github")
    async def webhook(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        event = request.headers.get("x-github-event", "")
        delivery_id = request.headers.get("x-github-delivery", "")

        # The signature covers the raw bytes, so nothing is parsed before it's verified.
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

        try:
            decision = await decide(deps.db, event, payload, delivery_id)
        except Exception as error:
            warn("could not record delivery", delivery_id=delivery_id, event=event, error=str(error))
            # A 5xx marks the delivery failed in GitHub, where it can be redelivered by hand.
            return JSONResponse({"error": "could not record delivery"}, status_code=502)
        if isinstance(decision, Ignore):
            return JSONResponse({"ignored": decision.reason})
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
        return JSONResponse({"run_id": run_id, "run": decision.run}, status_code=202)

    return app
