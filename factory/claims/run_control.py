"""RunControl on the Render API: task runs, flow runs, and sandboxes."""

from __future__ import annotations

from render import RenderAsync
from render.client.errors import ClientError

from factory.claims.status import RunNotFoundError
from factory.github import RunKind


class RenderRunControl:
    """Reads RENDER_API_KEY from the environment."""

    def __init__(self, render: RenderAsync | None = None) -> None:
        self._render = render or RenderAsync()

    async def get_status(self, run_id: str, kind: RunKind) -> str:
        try:
            if kind == "flow":
                return str((await self._render.workflows.get_flow_run(run_id)).status)
            return str((await self._render.workflows.get_task_run(run_id)).status)
        except ClientError as error:
            if error.status_code == 404:
                raise RunNotFoundError(run_id) from error
            raise

    async def cancel(self, run_id: str, kind: RunKind) -> None:
        if kind == "flow":
            await self._render.workflows.cancel_flow_run(run_id)
        else:
            await self._render.workflows.cancel_task_run(run_id)

    async def terminate_sandbox(self, sandbox_id: str) -> None:
        try:
            await self._render.sandboxes.terminate(sandbox_id)
        except ClientError as error:
            if error.status_code != 404:
                raise
