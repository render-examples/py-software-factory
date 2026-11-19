"""Gateway entry point: `python -m factory.gateway.main`. Binds 0.0.0.0:$PORT."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI
from render import RenderAsync

from factory.config import RunName, require_env
from factory.db import create_db, migrate
from factory.gateway.app import GatewayDeps, create_gateway
from factory.gateway.hub import EventHub, listen

GATEWAY_POOL_SIZE = 10


def build() -> Any:
    webhook_secret = require_env("GITHUB_WEBHOOK_SECRET")
    workflow_slug = require_env("RENDER_WORKFLOW_SLUG")
    events_read_token = require_env("EVENTS_READ_TOKEN")
    database_url = require_env("DATABASE_URL")
    db = create_db(database_url, GATEWAY_POOL_SIZE)
    hub = EventHub()
    listener = listen(hub, database_url)
    # Reads RENDER_API_KEY.
    render = RenderAsync()

    async def start_run(run: RunName, task_input: dict[str, Any], idempotency_key: str) -> str:
        slug = f"{workflow_slug}/{run}"
        return (await render.workflows.start_task(slug, [task_input], idempotency_key=idempotency_key)).id

    app = create_gateway(
        GatewayDeps(
            start_run=start_run,
            webhook_secret=webhook_secret,
            db=db,
            hub=hub,
            events_read_token=events_read_token,
        )
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        applied = await migrate(db)
        if applied:
            print(json.dumps({"level": "info", "msg": "applied migrations", "migrations": applied}), flush=True)
        await listener.start()
        try:
            yield
        finally:
            await listener.stop()
            await db.aclose()

    app.router.lifespan_context = lifespan
    return app


def main() -> None:
    port = int(os.environ.get("PORT", "3000"))
    uvicorn.run(build(), host="0.0.0.0", port=port)  # noqa: S104 - Render routes to 0.0.0.0:$PORT


if __name__ == "__main__":
    main()
