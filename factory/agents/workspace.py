"""The agents' Pydantic AI workspace: commands in the factory's sandbox, run as the unprivileged user.

It offers commands only. File tools send operations to the sandbox's file helper instead (`SandboxFiles`), so
files never travel through this backend.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic_ai.workspaces import CommandResult, WorkspaceCommand, WorkspaceRef

from factory.sandbox import REPO_DIR, Sandbox, shq

DEFAULT_COMMAND_SECONDS = 30
MAX_COMMAND_SECONDS = 30 * 60
KILL_GRACE_SECONDS = 5


class SandboxWorkspace:
    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox
        self.ref = WorkspaceRef(provider="render", id=sandbox.id)

    async def working_dir(self) -> str:
        return REPO_DIR

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - Pydantic AI workspace contract
    ) -> CommandResult:
        if isinstance(command, str):
            text = command if shell else shq(command)
        else:
            text = " ".join(shq(part) for part in command)
        if env:
            # Every command runs under `bash -c`, so an export also reaches shell builtins such as cd.
            text = f"export {' '.join(f'{key}={shq(value)}' for key, value in sorted(env.items()))}\n{text}"
        seconds = min(timeout or DEFAULT_COMMAND_SECONDS, MAX_COMMAND_SECONDS)
        # The sandbox enforces the limit, so a command that runs over ends with exit 124 and its output, rather than
        # outliving a client-side timeout. Short limits keep the command on the single-request streaming exec.
        limited = f"timeout --kill-after={KILL_GRACE_SECONDS}s {seconds:g} bash -c {shq(text)}"
        result = await self.sandbox.exec(limited, timeout_seconds=seconds + KILL_GRACE_SECONDS, as_runner=True)
        return CommandResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)
