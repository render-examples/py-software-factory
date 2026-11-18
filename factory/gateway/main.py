"""Gateway entry point: `python -m factory.gateway.main`. Binds 0.0.0.0:$PORT."""

from __future__ import annotations

import os
from typing import Any

import uvicorn
from render import RenderAsync

from factory.config import RunName, require_env
from factory.gateway.app import GatewayDeps, create_gateway


def build() -> Any:
    webhook_secret = require_env("GITHUB_WEBHOOK_SECRET")
    workflow_slug = require_env("RENDER_WORKFLOW_SLUG")
    # Reads RENDER_API_KEY.
    render = RenderAsync()

    async def start_run(run: RunName, task_input: dict[str, Any], idempotency_key: str) -> str:
        slug = f"{workflow_slug}/{run}"
        return (await render.workflows.start_task(slug, [task_input], idempotency_key=idempotency_key)).id

    return create_gateway(GatewayDeps(start_run=start_run, webhook_secret=webhook_secret))


def main() -> None:
    port = int(os.environ.get("PORT", "3000"))
    uvicorn.run(build(), host="0.0.0.0", port=port)  # noqa: S104 - Render routes to 0.0.0.0:$PORT


if __name__ == "__main__":
    main()
