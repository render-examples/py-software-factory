from __future__ import annotations

from typing import Protocol

from render import RenderAsync
from render.client.errors import ClientError

from factory.sandbox.client import sandbox_client


class RunNotFoundError(Exception): ...


class RunControl(Protocol):
    async def get_status(self, run_id: str) -> str: ...

    async def cancel(self, run_id: str) -> None: ...

    async def terminate_sandbox(self, sandbox_id: str) -> None: ...


class SandboxTerminator(Protocol):
    async def terminate(self, sandbox_id: str) -> None: ...


class RenderRunControl:
    def __init__(
        self,
        render: RenderAsync | None = None,
        sandboxes: SandboxTerminator | None = None,
    ) -> None:
        self._render = render or RenderAsync()
        self._sandboxes = sandboxes or sandbox_client()

    async def get_status(self, run_id: str) -> str:
        try:
            return str((await self._render.workflows.get_task_run(run_id)).status)
        except ClientError as error:
            if error.status_code == 404:
                raise RunNotFoundError(run_id) from error
            raise

    async def cancel(self, run_id: str) -> None:
        await self._render.workflows.cancel_task_run(run_id)

    async def terminate_sandbox(self, sandbox_id: str) -> None:
        await self._sandboxes.terminate(sandbox_id)
