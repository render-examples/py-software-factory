from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from factory.config import RepoContext


class Snapshot(Protocol):
    @property
    def id(self) -> str: ...


BuildSnapshot = Callable[[RepoContext, str], Awaitable[Snapshot]]


@dataclass(frozen=True)
class SnapshotResult:
    snapshot_id: str
    repo: str
    sha: str


async def refresh_snapshot_stage(repo: RepoContext, sha: str, build_snapshot: BuildSnapshot) -> SnapshotResult:
    built = await build_snapshot(repo, sha)
    return SnapshotResult(built.id, repo.full_name, sha)
