from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

import pytest
from render.workflows.context import TaskRunMetadata

from factory.agents.registry import IMPLEMENTER
from factory.claims import ClaimOptions, RunIdentity, TaskResult
from factory.config import require_repo
from factory.github import Actor, IssueComment, StatusMarker, render_marker
from factory.schemas import AutopilotInput, TriageInput
from factory.stages import cancel_stage
from factory.workflows.app import app
from factory.workflows.autopilot import AutopilotDeps, autopilot_stage
from factory.workflows.refresh_snapshot import refresh_snapshot_run
from factory.workflows.services import effective_placement, model_egress
from factory.workflows.triage import run_triage
from tests.fixtures import REPO

SHA = "a" * 40


def result(outcome: str, data: object = None, *, state: str = "done") -> dict[str, object]:
    return {
        "state": state,
        "outcome": outcome,
        "summary": outcome,
        "trace_id": "root",
        "run_id": f"run-{outcome}",
        "data": data,
    }


class AutopilotGitHub:
    def __init__(self) -> None:
        self.labels: list[tuple[str, int]] = []

    async def remove_label(self, number: int, label: str) -> None:
        self.labels.append((f"-{label}", number))

    async def add_labels(self, number: int, labels: list[str]) -> None:
        self.labels.extend((f"+{label}", number) for label in labels)


class AutopilotClaim:
    def __init__(self) -> None:
        self.gh = AutopilotGitHub()
        self.repo = require_repo(REPO, 1)
        self.progress: list[str] = []

    async def report_progress(self, summary: str) -> None:
        self.progress.append(summary)

    async def assert_current(self) -> None:
        return None


class ChildContext:
    metadata = TaskRunMetadata(task_run_id="root")


class NamedTask(Protocol):
    name: str


async def test_autopilot_orders_children_and_hands_off_typed_json() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def run_child(task: object, payload: object) -> object:
        name = cast(NamedTask, task).name
        body = cast(dict[str, Any], payload)
        calls.append((name, body))
        if name == "triage":
            return result(
                "drafted",
                {"kind": "spec", "comment_id": 11, "hash": "f" * 64, "version": 2},
            )
        if name == "spec-to-pr":
            return result("pr_opened", {"kind": "pull_request", "pr": 23, "head_sha": SHA})
        return result("reviewed")

    input = AutopilotInput(repo=REPO, installation_id=1, issue=7, actor="maintainer")
    outcome = await autopilot_stage(
        AutopilotClaim(),  # type: ignore[arg-type]
        input,
        ChildContext(),  # type: ignore[arg-type]
        AutopilotDeps(run_child),
    )
    assert outcome.outcome == "completed"
    assert [name for name, _ in calls] == ["triage", "spec-to-pr", "pr-review"]
    assert calls[1][1]["approval"] == {
        "kind": "autopilot",
        "comment_id": 11,
        "hash": "f" * 64,
        "version": 2,
    }
    assert calls[2][1] == {
        "repo": REPO,
        "installation_id": 1,
        "actor": "maintainer",
        "pr": 23,
        "head_sha": SHA,
        "autopilot_issue": 7,
    }


async def test_autopilot_questions_restore_the_label_and_stop() -> None:
    claim = AutopilotClaim()

    async def ask(_task: object, _payload: object) -> object:
        return result("asked")

    outcome = await autopilot_stage(
        claim,  # type: ignore[arg-type]
        AutopilotInput(repo=REPO, installation_id=1, issue=7, actor="maintainer"),
        ChildContext(),  # type: ignore[arg-type]
        AutopilotDeps(ask),
    )
    assert outcome.outcome == "waiting_for_answers"
    assert claim.gh.labels[-1] == ("+factory:autopilot", 7)


async def test_child_github_task_continues_the_root_claim(monkeypatch: Any) -> None:
    captured: dict[str, object] = {}

    async def fake_with_claim(identity: RunIdentity, options: object, work: object) -> TaskResult[None]:
        captured["identity"] = identity
        captured["options"] = options
        return TaskResult("done", "ok", "ok", identity.trace_id, identity.run_id)

    monkeypatch.setattr("factory.workflows.github_task.with_claim", fake_with_claim)

    class Context:
        metadata = TaskRunMetadata(
            task_run_id="child",
            root_task_run_id="root",
            parent_task_run_id="root",
        )

    await run_triage(
        Context(),  # type: ignore[arg-type]
        TriageInput(repo=REPO, installation_id=1, issue=7, actor="maintainer").model_dump(),
        gh=object(),  # type: ignore[arg-type]
        runs=object(),  # type: ignore[arg-type]
    )
    assert captured["identity"] == RunIdentity("root", "child")
    assert cast(ClaimOptions, captured["options"]).mode == "continue"


class CancelGitHub:
    def __init__(self) -> None:
        marker = render_marker(
            StatusMarker(
                run_id="child",
                trace_id="root",
                root_stage="autopilot",
                stage="pr-review",
                title="PR Reviewer",
                state="running",
                sandbox_id="sandbox",
            )
        )
        self.comment = IssueComment(1, marker, Actor("software-factory[bot]", "Bot"), "", "", "url")
        self.updated = ""

    async def get_permission(self, login: str) -> str:
        return "write"

    async def bot_login(self) -> str:
        return "software-factory[bot]"

    async def list_issue_comments(self, number: int) -> list[IssueComment]:
        return [self.comment]

    async def update_comment(self, comment_id: int, body: str) -> IssueComment:
        self.updated = body
        return self.comment

    async def create_comment(self, number: int, body: str) -> IssueComment:
        raise AssertionError("status exists")

    async def ensure_label(self, name: str, color: str, description: str = "") -> None:
        return None

    async def add_labels(self, number: int, labels: list[str]) -> None:
        return None

    async def remove_label(self, number: int, label: str) -> None:
        return None


@dataclass
class CancelRuns:
    cancelled: list[str]
    terminated: list[str]

    async def get_status(self, run_id: str) -> str:
        return "running"

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    async def terminate_sandbox(self, sandbox_id: str) -> None:
        self.terminated.append(sandbox_id)


async def test_cancel_stops_root_and_recorded_child_sandbox() -> None:
    gh = CancelGitHub()
    runs = CancelRuns([], [])
    cancelled = await cancel_stage(
        gh=gh,
        runs=runs,
        repo=require_repo(REPO, 1),
        number=7,
        kind="issue",
        actor="maintainer",
        identity=RunIdentity("cancel", "cancel"),
    )
    assert cancelled.cancelled is True
    assert cancelled.run_id == "root"
    assert cancelled.current_run_id == "child"
    assert runs.cancelled == ["root"]
    assert runs.terminated == ["sandbox"]
    assert "cancelled by @maintainer" in gh.updated


def test_one_app_registers_exactly_six_tasks() -> None:
    assert sorted(app._registry._tasks) == [
        "autopilot",
        "cancel",
        "pr-review",
        "refresh-snapshot",
        "spec-to-pr",
        "triage",
    ]
    assert all(
        task.options.retry is not None and task.options.retry.max_retries == 0 for task in app._registry._tasks.values()
    )


def test_broker_setting_controls_model_placement_and_egress_together() -> None:
    assert effective_placement(IMPLEMENTER, False) == "in-task"
    assert model_egress(IMPLEMENTER, False) == ()
    assert effective_placement(IMPLEMENTER, True) == "in-sandbox"
    assert model_egress(IMPLEMENTER, True) == ("api.anthropic.com",)


async def test_snapshot_refresh_checks_the_push_actors_current_permission() -> None:
    class NoPermission:
        async def get_permission(self, login: str) -> str:
            return "read"

    with pytest.raises(PermissionError, match="permission"):
        await refresh_snapshot_run(
            {
                "repo": REPO,
                "installation_id": 1,
                "sha": SHA,
                "actor": "reader",
            },
            gh=cast(Any, NoPermission()),
        )
