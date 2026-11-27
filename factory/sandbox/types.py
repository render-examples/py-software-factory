"""The sandbox interface the rest of the factory programs against, and fixed paths inside every sandbox."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

SANDBOX_ROOT: Final = "/opt/factory"
"""The snapshot build creates these paths; restored sandboxes find them in place."""
APP_DIR: Final = f"{SANDBOX_ROOT}/app"
"""Scripts, env file, and manifest. Root-owned, read-only to the runner user."""
SANDBOX_PYTHON: Final = f"{SANDBOX_ROOT}/venv/bin/python"
"""The sandbox's own Python 3.12, which runs the file helper."""
REPO_DIR: Final = f"{SANDBOX_ROOT}/repo"
"""The repository checkout. Owned by the runner user, so agents with write tools can edit it."""
SCRATCH_DIR: Final = f"{SANDBOX_ROOT}/scratch"
"""Scratch space for the runner user (HOME and TMPDIR)."""
RUN_AS: Final = "factory"
"""Unprivileged user agents and repository code run as."""
RUNNER_HOME: Final = f"{SCRATCH_DIR}/home"
RUNNER_TMP: Final = f"{SCRATCH_DIR}/tmp"  # noqa: S108 - isolated inside the sandbox
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
    """The factory scripts the snapshot was built with. A deployment with different scripts rebuilds it."""
    install: str | None
    created_at: str


class Sandbox(Protocol):
    """A running sandbox."""

    @property
    def id(self) -> str: ...

    async def exec(self, command: str, *, timeout_seconds: float | None = None, as_runner: bool = False) -> ExecResult:
        """Run a command, selecting a proxy-safe transport from its timeout.

        `as_runner` runs as RUN_AS from REPO_DIR with a scrubbed environment,
        instead of as root.
        """
        ...

    async def upload(self, path: str, data: bytes) -> None: ...

    async def download(self, path: str) -> bytes: ...

    async def close(self) -> None:
        """Idempotent: a sandbox that no longer exists counts as closed."""
        ...


class OpenedSandbox(Sandbox, Protocol):
    head_sha: str
    manifest: SnapshotManifest
