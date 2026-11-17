"""Registered cancellation task."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from render.workflows import TaskContext

from factory.claims import RunControl
from factory.claims.run_control import RenderRunControl
from factory.claims.trace import task_identity
from factory.config import Tasks, require_repo
from factory.github import GitHubClient, github_for
from factory.schemas import CancelInput
from factory.stages import cancel_stage
from factory.workflows.app import app


async def cancel_run(
    ctx: TaskContext,
    raw: object,
    *,
    gh: GitHubClient | None = None,
    runs: RunControl | None = None,
) -> dict[str, Any]:
    input = CancelInput.model_validate(raw)
    repo = require_repo(input.repo, input.installation_id)
    client = gh or github_for(repo)
    try:
        result = await cancel_stage(
            gh=client,
            runs=runs or RenderRunControl(),
            repo=repo,
            number=input.number,
            kind=input.kind,
            actor=input.actor,
            identity=task_identity(ctx.metadata),
        )
        return asdict(result)
    finally:
        if gh is None:
            await client.aclose()


async def _cancel_entry(ctx: TaskContext, raw: object) -> dict[str, Any]:
    return await cancel_run(ctx, raw)


cancel_task = app.task(name=Tasks.CANCEL, timeout_seconds=120, retry=None)(_cancel_entry)
