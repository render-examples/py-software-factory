"""Shared registration and validation for GitHub-attached tasks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Generic, TypeVar, cast

from pydantic import ValidationError
from render.workflows import TaskContext, TaskDefinition

from factory.config import require_repo
from factory.db import Db
from factory.events import warn
from factory.events.catalog import RunMode
from factory.github import GitHubClient
from factory.runs import (
    Outcome,
    RenderRunControl,
    Run,
    RunControl,
    RunOptions,
    TaskResult,
    TerminalState,
    task_identity,
    with_run,
)
from factory.schemas.tasks import RepoInput
from factory.threads import GitHubThread
from factory.workflows.app import app

InputT = TypeVar("InputT", bound=RepoInput)
DataT = TypeVar("DataT")


StageWork = Callable[[Run, InputT, TaskContext], Awaitable[Outcome[DataT]]]


def _no_reply(_task_input: object) -> str | None:
    return None


def _no_trigger(_task_input: object) -> Trigger | None:
    return None


@dataclass(frozen=True)
class Trigger:
    label: str
    number: int


@dataclass(frozen=True)
class GitHubTaskSpec(Generic[InputT]):
    name: str
    title: str
    input_type: type[InputT]
    thread: Callable[[InputT], GitHubThread]
    timeout_seconds: int
    reply_to: Callable[[InputT], str | None] = _no_reply
    """The waiting run a root run answers; it then starts in reply mode."""
    trigger: Callable[[InputT], Trigger | None] = _no_trigger
    """The label that started a root run. Labels are commands: it is removed once the run has started."""


@dataclass(frozen=True)
class GitHubTask(Generic[InputT, DataT]):
    task: TaskDefinition[..., dict[str, Any]]
    run: Callable[..., Awaitable[dict[str, Any]]]


def define_github_task(spec: GitHubTaskSpec[InputT], work: StageWork[InputT, DataT]) -> GitHubTask[InputT, DataT]:
    async def run(
        ctx: TaskContext,
        raw: object,
        *,
        gh: GitHubClient | None = None,
        runs: RunControl | None = None,
        db: Db | None = None,
    ) -> dict[str, Any]:
        task_input = spec.input_type.model_validate(raw)
        repo = require_repo(task_input.repo, task_input.installation_id)
        identity = task_identity(ctx.metadata)
        reply_to = spec.reply_to(task_input) if identity.is_root else None
        trigger = spec.trigger(task_input) if identity.is_root else None
        mode: RunMode = "continue" if not identity.is_root else "reply" if reply_to else "replace"
        options = RunOptions(
            repo=repo,
            thread=spec.thread(task_input),
            task=spec.name,
            title=spec.title,
            actor=task_input.actor,
            runs=(lambda: runs) if runs is not None else RenderRunControl,
            mode=mode,
            reply_to=reply_to,
            gh=gh,
            db=db,
        )

        async def started(thread_run: Run) -> Outcome[DataT]:
            if trigger is not None:
                try:
                    await thread_run.gh.remove_label(trigger.number, trigger.label)
                except Exception as error:
                    warn(
                        "could not remove the trigger label",
                        label=trigger.label,
                        number=trigger.number,
                        error=str(error),
                    )
            return await work(thread_run, task_input, ctx)

        result = await with_run(identity, options, started)
        return task_result_json(result)

    async def registered_entry(ctx: TaskContext, raw: object) -> dict[str, Any]:
        return await run(ctx, raw)

    registered = app.task(name=spec.name, timeout_seconds=spec.timeout_seconds, retry=None)(registered_entry)
    return GitHubTask(registered, run)


def task_result_json(result: TaskResult[Any]) -> dict[str, Any]:
    """Only JSON values cross a task boundary; dataclasses never do."""
    return asdict(result)


def parse_task_result(raw: object) -> TaskResult[Any]:
    if not isinstance(raw, dict):
        raise TypeError("child task returned a non-object result")
    result = cast(dict[str, Any], raw)
    required = {"state", "outcome", "summary", "trace_id", "run_id"}
    if not required.issubset(result):
        raise ValueError("child task returned an incomplete result")
    state = result["state"]
    if state not in {"done", "failed", "cancelled", "rejected"}:
        raise ValueError(f"child task returned invalid state {state!r}")
    return TaskResult(
        state=cast(TerminalState, state),
        outcome=str(result["outcome"]),
        summary=str(result["summary"]),
        trace_id=str(result["trace_id"]),
        run_id=str(result["run_id"]),
        data=result.get("data"),
    )


__all__ = [
    "GitHubTask",
    "GitHubTaskSpec",
    "Trigger",
    "ValidationError",
    "define_github_task",
    "parse_task_result",
    "task_result_json",
]
