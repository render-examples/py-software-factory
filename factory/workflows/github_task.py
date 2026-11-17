"""Shared registration and validation for GitHub-attached tasks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Generic, TypeVar, cast

from pydantic import ValidationError
from render.workflows import TaskContext, TaskDefinition

from factory.claims import Claim, ClaimOptions, Outcome, RunControl, Subject, TaskResult, with_claim
from factory.claims.run_control import RenderRunControl
from factory.claims.trace import task_identity
from factory.config import require_repo
from factory.github import GitHubClient
from factory.schemas import TaskInput
from factory.workflows.app import app

InputT = TypeVar("InputT", bound=TaskInput)
DataT = TypeVar("DataT")


SubjectFactory = Callable[[InputT, bool], Subject]
StageWork = Callable[[Claim, InputT, TaskContext], Awaitable[Outcome[DataT]]]


@dataclass(frozen=True)
class GitHubTaskSpec(Generic[InputT]):
    name: str
    title: str
    input_type: type[InputT]
    subject: SubjectFactory[InputT]
    timeout_seconds: int


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
    ) -> dict[str, Any]:
        input = spec.input_type.model_validate(raw)
        github_input = cast(Any, input)
        repo = require_repo(github_input.repo, github_input.installation_id)
        identity = task_identity(ctx.metadata)
        is_child = identity.trace_id != identity.run_id
        actor = str(github_input.actor)
        options = ClaimOptions(
            repo=repo,
            subject=spec.subject(input, is_child),
            task=spec.name,
            title=spec.title,
            actor=actor,
            runs=runs or RenderRunControl(),
            mode="continue" if is_child else "replace",
            gh=gh,
        )
        result = await with_claim(identity, options, lambda claim: work(claim, input, ctx))
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
    return TaskResult(
        state=result["state"],  # type: ignore[arg-type]
        outcome=str(result["outcome"]),
        summary=str(result["summary"]),
        trace_id=str(result["trace_id"]),
        run_id=str(result["run_id"]),
        data=result.get("data"),
    )


__all__ = [
    "GitHubTask",
    "GitHubTaskSpec",
    "ValidationError",
    "define_github_task",
    "parse_task_result",
    "task_result_json",
]
