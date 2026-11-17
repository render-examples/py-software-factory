from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from render.client.errors import ClientError
from render.workflows.context import TaskRunMetadata

from factory.claims import RunNotFoundError, task_identity
from factory.claims.run_control import RenderRunControl


class FakeWorkflows:
    def __init__(self) -> None:
        self.status = "running"
        self.cancelled: list[str] = []
        self.missing = False

    async def get_task_run(self, run_id: str) -> Any:
        if self.missing:
            raise ClientError("missing", status_code=404)
        return SimpleNamespace(status=self.status)

    async def cancel_task_run(self, run_id: str) -> None:
        self.cancelled.append(run_id)


class FakeSandboxes:
    def __init__(self) -> None:
        self.terminated: list[str] = []
        self.missing = False

    async def terminate(self, sandbox_id: str) -> None:
        if self.missing:
            raise ClientError("missing", status_code=404)
        self.terminated.append(sandbox_id)


class FakeRender:
    def __init__(self) -> None:
        self.workflows = FakeWorkflows()
        self.sandboxes = FakeSandboxes()


def test_task_identity_uses_the_root_metadata_and_has_a_stable_local_fallback() -> None:
    child = task_identity(TaskRunMetadata(task_run_id="trn-child", root_task_run_id="trn-root"))
    assert (child.trace_id, child.run_id) == ("trn-root", "trn-child")

    root = task_identity(TaskRunMetadata(task_run_id="trn-root"))
    assert (root.trace_id, root.run_id) == ("trn-root", "trn-root")

    local = task_identity(None)
    assert local.trace_id == local.run_id
    assert local.run_id


async def test_render_run_control_uses_task_apis_and_treats_missing_resources_as_gone() -> None:
    render = FakeRender()
    control = RenderRunControl(cast(Any, render))

    render.workflows.status = "paused"
    assert await control.get_status("trn-1") == "paused"
    await control.cancel("trn-1")
    await control.terminate_sandbox("sbx-1")
    assert render.workflows.cancelled == ["trn-1"]
    assert render.sandboxes.terminated == ["sbx-1"]

    render.workflows.missing = True
    with pytest.raises(RunNotFoundError):
        await control.get_status("trn-missing")

    render.sandboxes.missing = True
    await control.terminate_sandbox("sbx-missing")
