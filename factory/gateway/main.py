from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI
from render import RenderAsync

from factory import tracing
from factory.config import RunName, require_env
from factory.db import create_db, migrate
from factory.gateway.app import GatewayDeps, create_gateway

GATEWAY_POOL_SIZE = 10


def build() -> Any:
    webhook_secret = require_env("GITHUB_WEBHOOK_SECRET")
    workflow_slug = require_env("RENDER_WORKFLOW_SLUG")
    db = create_db(require_env("DATABASE_URL"), GATEWAY_POOL_SIZE)
    render = RenderAsync()

    async def start_run(run: RunName, task_input: dict[str, Any], idempotency_key: str) -> str:
        context = tracing.carrier()
        traced = {**task_input, "trace_context": context} if context else task_input
        slug = f"{workflow_slug}/{run}"
        return (await render.workflows.start_task(slug, [traced], idempotency_key=idempotency_key)).id

    app = create_gateway(GatewayDeps(start_run=start_run, webhook_secret=webhook_secret, db=db))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        applied = await migrate(db)
        if applied:
            print(json.dumps({"level": "info", "msg": "applied migrations", "migrations": applied}), flush=True)
        try:
            yield
        finally:
            await db.aclose()

    app.router.lifespan_context = lifespan
    tracing.configure("py-factory-gateway")
    tracing.instrument_gateway(app)
    return app


def main() -> None:
    port = int(os.environ.get("PORT", "3000"))
    uvicorn.run(build(), host="0.0.0.0", port=port)  # noqa: S104


if __name__ == "__main__":
    main()
