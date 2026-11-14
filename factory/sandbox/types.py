"""The sandbox interface the rest of the factory programs against, and fixed paths inside every sandbox."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

SANDBOX_ROOT: Final = "/opt/factory"
"""The snapshot build creates these paths; restored sandboxes find them in place."""
APP_DIR: Final = f"{SANDBOX_ROOT}/app"
"""Runner, scripts, env file, and manifest. Root-owned, read-only to the runner user."""
REPO_DIR: Final = f"{SANDBOX_ROOT}/repo"
"""The repository checkout. Owned by the runner user, so agents with write tools can edit it."""
SCRATCH_DIR: Final = f"{SANDBOX_ROOT}/scratch"
"""Scratch space for the runner user (HOME and TMPDIR)."""
RUN_AS: Final = "factory"
"""Unprivileged user agents and repository code run as."""


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


class Sandbox(Protocol):
    """A running sandbox."""

    @property
    def id(self) -> str: ...

    async def exec(self, command: str, *, timeout_seconds: float | None = None, as_runner: bool = False) -> ExecResult:
        """`as_runner` runs as RUN_AS from REPO_DIR with a scrubbed environment, instead of as root."""
        ...

    async def upload(self, path: str, data: bytes) -> None: ...

    async def download(self, path: str) -> bytes: ...

    async def close(self) -> None:
        """Idempotent: a sandbox that no longer exists counts as closed."""
        ...
