"""Permission-checked cancellation of the root run recorded in a status comment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from factory.claims import (
    RunControl,
    RunIdentity,
    StatusGitHub,
    StatusUpdate,
    check_permission,
    finish_status,
    read_status,
    stop_run,
)
from factory.claims.permission import PermissionGitHub
from factory.claims.status import StopContext
from factory.config import RepoContext, get_config


class CancelGitHub(StatusGitHub, PermissionGitHub, Protocol):
    pass


@dataclass(frozen=True)
class CancelResult:
    cancelled: bool
    run_id: str | None = None
    current_run_id: str | None = None
    reason: Literal["permission_denied", "nothing_running", "already_stopped"] | None = None


async def cancel_stage(
    *,
    gh: CancelGitHub,
    runs: RunControl,
    repo: RepoContext,
    number: int,
    kind: Literal["issue", "pr"],
    actor: str,
    identity: RunIdentity,
) -> CancelResult:
    if not await check_permission(gh, actor):
        await gh.create_comment(
            number,
            f"@{actor} needs `{get_config().minimum_permission}` permission on this repository to cancel factory runs.",
        )
        return CancelResult(False, reason="permission_denied")
    current = await read_status(gh, number)
    active = False
    if current:
        active = await stop_run(
            runs,
            current[1],
            StopContext(repo.full_name, f"{kind}#{number}", identity.run_id, "cancelled"),
        )
    if current is None or (not active and current[1].state != "running"):
        await gh.create_comment(number, f"@{actor} no factory run is in progress here.")
        return CancelResult(False, reason="nothing_running")

    marker = current[1]
    root_run = marker.root_run_id
    child_running = marker.state == "running"
    if child_running:
        displayed_run = marker.run_id
        displayed_stage = marker.stage
        title = marker.title or marker.stage
    else:
        displayed_run = root_run
        displayed_stage = marker.root_stage or marker.stage
        title = marker.root_stage or marker.title or marker.stage
    if active:
        summary = f"cancelled by @{actor}."
    else:
        summary = f"cleared by @{actor}; the run had already stopped without finishing."
    await finish_status(
        gh,
        StatusUpdate(
            number=number,
            identity=RunIdentity(root_run, displayed_run),
            task=displayed_stage,
            title=title,
            state="cancelled",
            summary=summary,
            root_stage=marker.root_stage,
        ),
        root_finished=True,
    )
    current_run = marker.run_id if child_running and marker.run_id != root_run else None
    if active:
        return CancelResult(True, root_run, current_run)
    return CancelResult(False, root_run, current_run, "already_stopped")
