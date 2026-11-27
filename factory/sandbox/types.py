from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

SANDBOX_ROOT: Final = "/opt/factory"
APP_DIR: Final = f"{SANDBOX_ROOT}/app"
SANDBOX_PYTHON: Final = f"{SANDBOX_ROOT}/venv/bin/python"
REPO_DIR: Final = f"{SANDBOX_ROOT}/repo"
SCRATCH_DIR: Final = f"{SANDBOX_ROOT}/scratch"
RUN_AS: Final = "factory"
RUNNER_HOME: Final = f"{SCRATCH_DIR}/home"
RUNNER_TMP: Final = f"{SCRATCH_DIR}/tmp"  # noqa: S108
RUNNER_PATH: Final = (
    f"{SANDBOX_ROOT}/venv/bin:{SANDBOX_ROOT}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class BranchRef:
    name: str


@dataclass(frozen=True)
class PullRef:
    number: int
    head_sha: str
    base_branch: str | None = None


SandboxRef = BranchRef | PullRef


@dataclass(frozen=True)
class SnapshotManifest:
    repo: str
    base_branch: str
    sha: str
    lockfile_hash: str
    scripts_hash: str
    install: str | None
    created_at: str


class Sandbox(Protocol):
    @property
    def id(self) -> str: ...

    async def exec(
        self, command: str, *, timeout_seconds: float | None = None, as_runner: bool = False
    ) -> ExecResult: ...

    async def upload(self, path: str, data: bytes) -> None: ...

    async def download(self, path: str) -> bytes: ...

    async def close(self) -> None: ...


class OpenedSandbox(Sandbox, Protocol):
    head_sha: str
    manifest: SnapshotManifest
