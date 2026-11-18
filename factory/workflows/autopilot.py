"""Autopilot parent task composing the three registered child tasks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict, cast

from render.workflows import TaskDefinition

from factory.claims import Claim, Outcome, Subject, TaskResult
from factory.config import Tasks, get_config
from factory.schemas import AutopilotInput
from factory.workflows.github_task import GitHubTaskSpec, TaskContext, define_github_task, parse_task_result
from factory.workflows.pr_review import pr_review_task
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


@dataclass(frozen=True)
class AutopilotDeps:
    run_child: ChildRunner | None = None


async def autopilot_stage(
    claim: Claim,
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
    await claim.gh.remove_label(task_input.issue, get_config().labels.autopilot)
    await claim.report_progress("running Triage")
    triage = record(
        "triage",
        await child(
            triage_task,
            {**base, "issue": task_input.issue, "comment_id": task_input.comment_id},
        ),
    )
    triage_data = _json_object(triage.data)
    if triage.state == "done" and triage.outcome == "asked":
        await claim.assert_current()
        await claim.gh.add_labels(task_input.issue, [get_config().labels.autopilot])
        return finish("waiting_for_answers", triage.summary)
    if triage.state != "done" or triage_data is None or triage_data.get("kind") != "spec":
        return stop("triage", triage)

    await claim.report_progress("implementing the spec")
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

    await claim.report_progress("reviewing the draft pull request")
    review = record(
        "pr-review",
        await child(
            pr_review_task,
            {
                **base,
                "pr": pr_data["pr"],
                "head_sha": pr_data["head_sha"],
                "autopilot_issue": task_input.issue,
            },
        ),
    )
    if review.state != "done" or review.outcome not in {"reviewed", "already_reviewed"}:
        return stop("pr-review", review)
    return finish("completed", f"opened and reviewed draft PR #{pr_data['pr']}.")


autopilot = define_github_task(
    GitHubTaskSpec(
        name=Tasks.AUTOPILOT,
        title="Autopilot",
        input_type=AutopilotInput,
        subject=lambda task_input, _child: Subject("issue", task_input.issue),
        timeout_seconds=8 * 60 * 60,
    ),
    lambda claim, task_input, ctx: autopilot_stage(claim, task_input, ctx),
)

autopilot_task = autopilot.task
run_autopilot = autopilot.run


def _json_object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw = cast(dict[object, object], value)
    return {str(key): item for key, item in raw.items()}
