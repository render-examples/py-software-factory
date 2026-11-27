"""Registered cancellation task: a run like any other on the issue or PR, whose start stops the running trace."""

from __future__ import annotations

from render.workflows import TaskContext

from factory.config import Tasks
from factory.runs import Outcome, Run
from factory.schemas import CancelInput
from factory.stages import cancel_outcome
from factory.threads import GitHubThread
from factory.workflows.github_task import GitHubTaskSpec, define_github_task


async def _cancel(run: Run, _task_input: CancelInput, _ctx: TaskContext) -> Outcome[None]:
    return cancel_outcome(run)


cancel = define_github_task(
    GitHubTaskSpec(
        name=Tasks.CANCEL,
        title="Cancel",
        input_type=CancelInput,
        thread=lambda task_input: GitHubThread(task_input.kind, task_input.number),
        timeout_seconds=120,
    ),
    _cancel,
)

cancel_task = cancel.task
run_cancel = cancel.run
