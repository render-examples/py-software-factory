from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

import logfire
import pytest
from logfire.testing import CaptureLogfire
from opentelemetry import trace
from render import TaskContext
from render.workflows.context import TaskRunMetadata

from factory.config import require_repo
from factory.db import Db
from factory.github import GitHubClient, Permission
from factory.runs import WORKING_LABEL, Outcome, Run, RunIdentity, RunOptions, TaskResult
from factory.runs.thread import StartingRun, start_on_thread
from factory.schemas import AutopilotInput, TriageInput
from factory.schemas.tasks import TraceContext
from factory.threads import GitHubThread, github_thread
from factory.workflows.app import app
from factory.workflows.autopilot import AutopilotDeps, ChildTask, autopilot_stage
from factory.workflows.cancel import run_cancel
from factory.workflows.refresh_snapshot import refresh_snapshot_run
from factory.workflows.triage import run_triage
from tests.fakes import FakeGitHub, FakeRuns
from tests.fixtures import REPO

SHA = "a" * 40


def result(outcome: str, data: Any = None, *, state: str = "done") -> dict[str, Any]:
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

    async def add_labels(self, number: int, labels: Sequence[str]) -> None:
        self.labels.extend((f"+{label}", number) for label in labels)


class AutopilotRun:
    def __init__(self) -> None:
        self.gh = AutopilotGitHub()
        self.repo = require_repo(REPO, 1)
        self.trace_id = "root"
        self.progress: list[str] = []

    async def report_progress(self, summary: str) -> None:
        self.progress.append(summary)

    async def assert_current(self) -> None:
        return None


class ChildContext:
    metadata = TaskRunMetadata(task_run_id="root")


class Started:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.fail = fail

    async def __call__(self, task: str, task_input: dict[str, Any], key: str) -> str:
        if self.fail:
            raise RuntimeError("Render is unavailable")
        self.calls.append((task, task_input, key))
        return "review-run"


async def autopilot(
    run_child: Callable[[ChildTask, dict[str, Any]], Awaitable[dict[str, Any]]],
    started: Started,
    run: AutopilotRun | None = None,
) -> Outcome[Any]:
    return await autopilot_stage(
        cast(Run, run or AutopilotRun()),
        AutopilotInput(repo=REPO, installation_id=1, issue=7, actor="maintainer"),
        cast(TaskContext, ChildContext()),
        AutopilotDeps(run_child, started),
    )


async def drafted_then_opened(task: ChildTask, payload: dict[str, Any]) -> dict[str, Any]:
    if task.name == "triage":
        return result("drafted", {"kind": "spec", "comment_id": 11, "hash": "f" * 64, "version": 2})
    return result("pr_opened", {"kind": "pull_request", "pr": 23, "head_sha": SHA})


async def test_autopilot_runs_triage_and_spec_to_pr_then_starts_the_review_on_the_pull_request() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def run_child(task: ChildTask, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((task.name, payload))
        return await drafted_then_opened(task, payload)

    started = Started()
    outcome = await autopilot(run_child, started)
    assert outcome.outcome == "pr_opened"
    assert [name for name, _ in calls] == ["triage", "spec-to-pr"]
    assert calls[1][1]["approval"] == {"kind": "autopilot", "comment_id": 11, "hash": "f" * 64, "version": 2}
    assert started.calls == [
        (
            "pr-review",
            {"repo": REPO, "installation_id": 1, "actor": "maintainer", "pr": 23, "head_sha": SHA},
            f"root:pr-review:{SHA}",
        )
    ]


async def test_autopilot_reports_a_review_that_could_not_start() -> None:
    outcome = await autopilot(drafted_then_opened, Started(fail=True))
    assert (outcome.state, outcome.outcome) == ("failed", "review_not_started")
    assert "factory:pr-review" in outcome.summary


@pytest.mark.parametrize("triage_outcome", ["asked", "skipped"])
async def test_autopilot_waits_for_a_reply_when_triage_asks_or_skips(triage_outcome: str) -> None:
    run = AutopilotRun()

    async def ask(_task: ChildTask, _payload: dict[str, Any]) -> dict[str, Any]:
        return result(triage_outcome)

    outcome = await autopilot(ask, Started(), run)
    assert (outcome.outcome, outcome.waiting) == ("waiting_for_answers", "reply")
    assert run.gh.labels == []


async def test_child_github_task_continues_the_root_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_with_run(
        identity: RunIdentity, options: RunOptions, work: Callable[[Run], Awaitable[Outcome[Any]]]
    ) -> TaskResult[Any]:
        del work
        captured["identity"] = identity
        captured["options"] = options
        return TaskResult("done", "ok", "ok", identity.trace_id, identity.run_id)

    monkeypatch.setattr("factory.workflows.github_task.with_run", fake_with_run)

    class Context:
        metadata = TaskRunMetadata(task_run_id="child", root_task_run_id="root", parent_task_run_id="root")

    await run_triage(
        cast(TaskContext, Context()),
        TriageInput(repo=REPO, installation_id=1, issue=7, actor="maintainer", reply_to="ignored").model_dump(),
        gh=cast(GitHubClient, object()),
    )
    assert captured["identity"] == RunIdentity("root", "child")
    options = cast(RunOptions, captured["options"])
    assert (options.mode, options.reply_to, options.thread) == ("continue", None, GitHubThread("issue", 7))


ISSUE_KEY = github_thread(REPO, GitHubThread("issue", 7))


def triggers_removed(gh: FakeGitHub) -> list[tuple[int, str]]:
    return [removed for removed in gh.removed_labels if removed[1] != WORKING_LABEL]


class TriageServices:
    def triage(self, _run: Run) -> None:
        return None


async def triage_with(db: Db, gh: FakeGitHub, run_id: str, **fields: Any) -> dict[str, Any]:
    class Context:
        metadata = TaskRunMetadata(task_run_id=run_id)

    raw = {"repo": REPO, "installation_id": 1, "issue": 7, "actor": "maintainer", **fields}
    return await run_triage(cast(TaskContext, Context()), raw, gh=gh.as_client(), runs=FakeRuns(), db=db)


@pytest.fixture
def quiet_triage(monkeypatch: pytest.MonkeyPatch) -> None:
    async def asked(run: Run, _task_input: TriageInput, _deps: object) -> Outcome[None]:
        await run.await_reply()
        return Outcome("asked", "asked 1 question.", waiting="reply")

    monkeypatch.setattr("factory.workflows.triage.get_services", TriageServices)
    monkeypatch.setattr("factory.workflows.triage.triage_stage", asked)


@pytest.mark.postgres
@pytest.mark.usefixtures("quiet_triage")
async def test_a_label_started_root_consumes_its_label_and_a_reply_started_one_does_not(db: Db) -> None:
    gh = FakeGitHub()
    first = await triage_with(db, gh, "trn-1")
    assert first["outcome"] == "asked"
    assert triggers_removed(gh) == [(7, "factory:triage")]

    reply = await triage_with(db, gh, "trn-2", comment_id=99, reply_to="trn-1")
    assert reply["state"] == "done"
    assert triggers_removed(gh) == [(7, "factory:triage")]


@pytest.mark.postgres
@pytest.mark.usefixtures("quiet_triage")
async def test_a_rejected_trigger_leaves_its_label(db: Db) -> None:
    gh = FakeGitHub(permissions={"maintainer": "read"})
    rejected = await triage_with(db, gh, "trn-1")
    assert rejected["state"] == "rejected"
    assert triggers_removed(gh) == []


async def hold_issue(db: Db, trace: str, *, sandbox: str | None = None) -> None:
    await start_on_thread(db, StartingRun(ISSUE_KEY, trace, trace, "autopilot", "Autopilot", "maintainer", "replace"))
    if sandbox:
        await db.query("update runs set sandbox_id = %s where run_id = %s", (sandbox, trace))


async def cancel(db: Db, gh: FakeGitHub, runs: FakeRuns, run_id: str) -> dict[str, Any]:
    class Context:
        metadata = TaskRunMetadata(task_run_id=run_id)

    raw = {"repo": REPO, "installation_id": 1, "actor": "maintainer", "number": 7, "kind": "issue"}
    return await run_cancel(cast(TaskContext, Context()), raw, gh=gh.as_client(), runs=runs, db=db)


@pytest.mark.postgres
async def test_cancel_stops_the_running_trace_and_its_sandbox(db: Db) -> None:
    gh, runs = FakeGitHub(), FakeRuns()
    await hold_issue(db, "root", sandbox="sandbox")
    cancelled = await cancel(db, gh, runs, "cancel-1")
    assert (cancelled["state"], cancelled["outcome"]) == ("done", "cancelled")
    assert runs.cancelled == ["root"]
    assert runs.terminated == ["sandbox"]
    assert "cancelled `root`" in gh.bodies(7)[0]


@pytest.mark.postgres
async def test_cancel_with_nothing_running_says_so(db: Db) -> None:
    gh, runs = FakeGitHub(), FakeRuns()
    cancelled = await cancel(db, gh, runs, "cancel-1")
    assert cancelled["outcome"] == "nothing_running"
    assert runs.cancelled == []


@pytest.mark.postgres
async def test_an_unconfirmed_cancel_asks_to_retry_and_the_retry_finishes_the_cleanup(db: Db) -> None:
    gh = FakeGitHub()
    await hold_issue(db, "root", sandbox="sandbox")
    first = await cancel(db, gh, FakeRuns(fail_cancel=True), "cancel-1")
    assert (first["state"], first["outcome"]) == ("failed", "cancel_unconfirmed")
    assert "/factory cancel" in first["summary"]
    second = await cancel(db, gh, FakeRuns(), "cancel-2")
    assert second["outcome"] == "cleaned_up"


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


async def test_autopilot_children_and_the_review_continue_its_trace() -> None:
    payloads: list[dict[str, Any]] = []

    async def run_child(task: ChildTask, payload: dict[str, Any]) -> dict[str, Any]:
        payloads.append(payload)
        return await drafted_then_opened(task, payload)

    started = Started()
    with logfire.span("autopilot"):
        trace_id = format(trace.get_current_span().get_span_context().trace_id, "032x")
        await autopilot(run_child, started)
    carried = [*payloads, started.calls[0][1]]
    assert [payload["trace_context"]["traceparent"].split("-")[1] for payload in carried] == [trace_id] * 3


async def test_a_task_continues_the_trace_its_input_carries(
    monkeypatch: pytest.MonkeyPatch, capfire: CaptureLogfire
) -> None:
    async def fake_with_run(
        identity: RunIdentity, options: RunOptions, work: Callable[[Run], Awaitable[Outcome[Any]]]
    ) -> TaskResult[Any]:
        del options, work
        return TaskResult("failed", "agent_stopped", "stopped", identity.trace_id, identity.run_id)

    monkeypatch.setattr("factory.workflows.github_task.with_run", fake_with_run)

    class Context:
        metadata = TaskRunMetadata(task_run_id="root", root_task_run_id="root", parent_task_run_id=None)

    parent = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"
    task_input = TriageInput(
        repo=REPO, installation_id=1, issue=7, actor="maintainer", trace_context=TraceContext(traceparent=parent)
    )
    await run_triage(cast(TaskContext, Context()), task_input.model_dump(), gh=cast(GitHubClient, object()))

    [span] = [
        span
        for span in capfire.exporter.exported_spans
        if span.name.startswith("{title}") and (span.attributes or {}).get("logfire.span_type") == "span"
    ]
    assert span.context is not None
    assert span.parent is not None
    assert (format(span.context.trace_id, "032x"), format(span.parent.span_id, "016x")) == ("1" * 32, "2" * 16)
    attributes = dict(span.attributes or {})
    assert (attributes["outcome"], attributes["factory.run_id"], attributes["factory.thread"]) == (
        "agent_stopped",
        "root",
        "issue#7",
    )


async def test_snapshot_refresh_checks_the_push_actors_current_permission() -> None:
    class NoPermission:
        async def get_permission(self, login: str) -> Permission:
            return "read"

        async def aclose(self) -> None:
            return None

    with pytest.raises(PermissionError, match="permission"):
        await refresh_snapshot_run(
            {
                "repo": REPO,
                "installation_id": 1,
                "sha": SHA,
                "actor": "reader",
            },
            gh=NoPermission(),
        )
