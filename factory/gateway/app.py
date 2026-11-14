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
from factory.events import warn
from factory.gateway.body import read_body
from factory.gateway.routes import Ignore, Invalid, route
from factory.gateway.signature import verify_signature

StartRun = Callable[[RunName, dict[str, Any], str], Awaitable[str]]
"""Starts a task or flow run with an idempotency key, and returns its run ID."""

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


def create_gateway(deps: GatewayDeps) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

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

        decision = route(event, payload, delivery_id)
        if isinstance(decision, Ignore):
            return JSONResponse({"ignored": decision.reason})
        if isinstance(decision, Invalid):
            warn(
                "authenticated delivery produced an invalid run input",
                delivery_id=delivery_id,
                event=event,
                run=decision.run,
                subject=decision.subject,
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
                subject=decision.subject,
                error=str(error),
            )
            # A 5xx marks the delivery failed in GitHub, where it can be redelivered by hand.
            return JSONResponse({"error": "could not start run", "run": decision.run}, status_code=502)
        return JSONResponse({"run_id": run_id, "run": decision.run}, status_code=202)

    return app
