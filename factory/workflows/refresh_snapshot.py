"""Registered base-branch snapshot refresh task."""

from __future__ import annotations

from dataclasses import asdict
from typing import Protocol

from render.workflows import TaskContext

from factory.claims import check_permission
from factory.claims.permission import PermissionGitHub
from factory.config import Tasks, require_repo
from factory.github import github_for
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
    return await refresh_snapshot_run(raw)


refresh_snapshot_task = app.task(name=Tasks.REFRESH_SNAPSHOT, timeout_seconds=60 * 60, retry=None)(_refresh_entry)
