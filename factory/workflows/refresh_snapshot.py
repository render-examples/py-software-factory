from __future__ import annotations

from dataclasses import asdict
from typing import Protocol

import logfire
from render.workflows import TaskContext

from factory import tracing
from factory.config import Tasks, require_repo
from factory.github import github_for
from factory.runs import PermissionGitHub, check_permission
from factory.schemas import RefreshSnapshotInput
from factory.stages import refresh_snapshot_stage
from factory.workflows.app import app
from factory.workflows.services import get_services


class RefreshGitHub(PermissionGitHub, Protocol):
    async def aclose(self) -> None: ...


async def refresh_snapshot_run(raw: object, *, gh: RefreshGitHub | None = None) -> dict[str, str]:
    task_input = RefreshSnapshotInput.model_validate(raw)
    repo = require_repo(task_input.repo, task_input.installation_id)
    client = gh or github_for(repo)
    try:
        if not await check_permission(client, task_input.actor):
            raise PermissionError(
                f"{task_input.actor} needs the configured repository permission to refresh a factory snapshot"
            )
        result = await refresh_snapshot_stage(repo, task_input.sha, get_services().build_snapshot)
        return asdict(result)
    finally:
        if gh is None:
            await client.aclose()


async def _refresh_entry(_ctx: TaskContext, raw: object) -> dict[str, str]:
    task_input = RefreshSnapshotInput.model_validate(raw)
    carried = task_input.trace_context.model_dump() if task_input.trace_context else None
    try:
        with (
            tracing.attached(carried),
            logfire.span("Refresh snapshot of {repo} at {sha}", repo=task_input.repo, sha=task_input.sha),
        ):
            return await refresh_snapshot_run(raw)
    finally:
        tracing.flush()


refresh_snapshot_task = app.task(name=Tasks.REFRESH_SNAPSHOT, timeout_seconds=60 * 60, retry=None)(_refresh_entry)
