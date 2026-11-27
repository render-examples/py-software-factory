from __future__ import annotations

import uuid
from dataclasses import dataclass

from render.workflows.context import TaskRunMetadata


@dataclass(frozen=True)
class RunIdentity:
    trace_id: str
    run_id: str

    @property
    def is_root(self) -> bool:
        return self.trace_id == self.run_id


def task_identity(metadata: TaskRunMetadata | None) -> RunIdentity:
    task_run_id = metadata.task_run_id if metadata else None
    root = metadata.root_task_run_id if metadata else None
    trace_id = root or task_run_id or str(uuid.uuid4())
    return RunIdentity(trace_id=trace_id, run_id=task_run_id or trace_id)
