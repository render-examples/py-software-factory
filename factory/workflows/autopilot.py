"""Autopilot parent task: Triage and Spec-to-PR as child tasks, then PR Reviewer as its own run on the pull
request."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict, cast

from render import RenderAsync
from render.workflows import TaskDefinition

from factory.config import Tasks, get_config, require_env
from factory.events import warn
from factory.runs import Outcome, Run, TaskResult
from factory.schemas import AutopilotInput
from factory.threads import GitHubThread
from factory.workflows.github_task import (
    GitHubTaskSpec,
    TaskContext,
    Trigger,
    define_github_task,
    parse_task_result,
)
from factory.workflows.spec_to_pr import spec_to_pr_task
from factory.workflows.triage import triage_task


class AutopilotStage(TypedDict):
    task: str
    run_id: str
    state: str
    outcome: str
    summary: str


class AutopilotData(TypedDict):
    stages: list[AutopilotStage]


ChildTask = TaskDefinition[..., dict[str, Any]]
ChildRunner = Callable[[ChildTask, dict[str, Any]], Awaitable[dict[str, Any]]]
StartTask = Callable[[str, dict[str, Any], str], Awaitable[str]]
"""Starts a registered task as a new root run with an idempotency key, and returns its run ID."""


async def start_task(task: str, task_input: dict[str, Any], idempotency_key: str) -> str:
    """Reads RENDER_API_KEY and RENDER_WORKFLOW_SLUG."""
    slug = f"{require_env('RENDER_WORKFLOW_SLUG')}/{task}"
    return (await RenderAsync().workflows.start_task(slug, [task_input], idempotency_key=idempotency_key)).id


@dataclass(frozen=True)
class AutopilotDeps:
    run_child: ChildRunner | None = None
    start_task: StartTask = start_task


async def autopilot_stage(
    run: Run,
    task_input: AutopilotInput,
    ctx: TaskContext,
    deps: AutopilotDeps = AutopilotDeps(),
) -> Outcome[AutopilotData]:
    stages: list[AutopilotStage] = []

    async def child(definition: ChildTask, payload: dict[str, Any]) -> TaskResult[Any]:
        if deps.run_child is not None:
            raw = await deps.run_child(definition, payload)
        else:
            raw = await ctx.run(definition, payload)
        return parse_task_result(raw)

    def record(name: str, result: TaskResult[Any]) -> TaskResult[Any]:
        stages.append(
            {
                "task": name,
                "run_id": result.run_id,
                "state": result.state,
                "outcome": result.outcome,
                "summary": result.summary,
            }
        )
        return result

    def finish(outcome: str, summary: str, state: Literal["done", "failed"] = "done") -> Outcome[AutopilotData]:
        return Outcome(outcome, summary, state=state, data={"stages": stages})

    def stop(name: str, result: TaskResult[Any]) -> Outcome[AutopilotData]:
        state: Literal["done", "failed"] = "done" if result.state == "done" else "failed"
        return finish(f"stopped_at_{name.replace('-', '_')}", result.summary, state)

    base = {
        "repo": task_input.repo,
        "installation_id": task_input.installation_id,
        "actor": task_input.actor,
    }
    await run.report_progress("running Triage")
    triage = record(
        "triage",
        await child(
            triage_task,
            {**base, "issue": task_input.issue, "comment_id": task_input.comment_id},
        ),
    )
    triage_data = _json_object(triage.data)
    if triage.state == "done" and triage.outcome in {"asked", "skipped"}:
        return Outcome("waiting_for_answers", triage.summary, data={"stages": stages}, waiting="reply")
    if triage.state != "done" or triage_data is None or triage_data.get("kind") != "spec":
        return stop("triage", triage)

    await run.report_progress("implementing the spec")
    spec_to_pr = record(
        "spec-to-pr",
        await child(
            spec_to_pr_task,
            {
                **base,
                "issue": task_input.issue,
                "approval": {
                    "kind": "autopilot",
                    "comment_id": triage_data["comment_id"],
                    "hash": triage_data["hash"],
                    "version": triage_data["version"],
                },
            },
        ),
    )
    pr_data = _json_object(spec_to_pr.data)
    if spec_to_pr.state != "done" or pr_data is None or pr_data.get("kind") != "pull_request":
        return stop("spec-to-pr", spec_to_pr)

    pr, head_sha = pr_data["pr"], pr_data["head_sha"]
    await run.assert_current()
    try:
        await deps.start_task(
            Tasks.PR_REVIEW, {**base, "pr": pr, "head_sha": head_sha}, f"{run.trace_id}:pr-review:{head_sha}"
        )
    except Exception as error:
        warn("could not start PR Reviewer", pr=pr, error=str(error))
        label = get_config().labels.pr_review
        summary = (
            f"opened draft PR #{pr}, but PR Reviewer couldn't start. Apply `{label}` to the pull request to review it."
        )
        return finish("review_not_started", summary, "failed")
    return finish("pr_opened", f"opened draft PR #{pr}; PR Reviewer is reviewing it on the pull request.")


autopilot = define_github_task(
    GitHubTaskSpec(
        name=Tasks.AUTOPILOT,
        title="Autopilot",
        input_type=AutopilotInput,
        thread=lambda task_input: GitHubThread("issue", task_input.issue),
        reply_to=lambda task_input: task_input.reply_to,
        trigger=lambda task_input: (
            Trigger(get_config().labels.autopilot, task_input.issue) if task_input.comment_id is None else None
        ),
        timeout_seconds=8 * 60 * 60,
    ),
    lambda run, task_input, ctx: autopilot_stage(run, task_input, ctx),
)

autopilot_task = autopilot.task
run_autopilot = autopilot.run


def _json_object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw = cast(dict[object, object], value)
    return {str(key): item for key, item in raw.items()}
