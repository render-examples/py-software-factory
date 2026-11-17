from __future__ import annotations

import uuid

from render.workflows.context import TaskRunMetadata

from factory.claims.status import RunIdentity


def task_identity(metadata: TaskRunMetadata | None) -> RunIdentity:
    """The trace is the root task run; local runs without metadata get a random one, which also serves as the run
    ID."""
    task_run_id = metadata.task_run_id if metadata else None
    root = metadata.root_task_run_id if metadata else None
    trace_id = root or task_run_id or str(uuid.uuid4())
    return RunIdentity(trace_id=trace_id, run_id=task_run_id or trace_id)
