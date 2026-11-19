from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from factory.agents import AgentError
from factory.config import require_repo
from factory.db import Db, read_stream
from factory.events.catalog import RunMode
from factory.runs import (
    WORKING_LABEL,
    ExpectedFailure,
    Outcome,
    RunIdentity,
    RunOptions,
    StatusLink,
    TaskResult,
    ThreadRun,
    sync_status,
    with_run,
)
from factory.runs.thread import StartingRun, cancel_if_active, start_on_thread
from factory.threads import GitHubThread, github_thread
from tests.fakes import FakeGitHub, FakeRuns
from tests.fixtures import REPO

pytestmark = pytest.mark.postgres

ISSUE = GitHubThread("issue", 7)
KEY = github_thread(REPO, ISSUE)


def options(
    gh: FakeGitHub,
    runs: FakeRuns,
    db: Db,
    *,
    mode: RunMode = "replace",
    reply_to: str | None = None,
    actor: str = "maintainer",
    task: str = "triage",
    title: str = "Triage",
) -> RunOptions:
    return RunOptions(
        repo=require_repo(REPO, 1),
        thread=ISSUE,
        task=task,
        title=title,
        actor=actor,
        runs=lambda: runs,
        mode=mode,
        reply_to=reply_to,
        gh=gh.as_client(),
        db=db,
    )


Work = Callable[[ThreadRun], Awaitable[Outcome[Any]]]


async def done(_run: ThreadRun) -> Outcome[Any]:
    return Outcome("ok", "did it.")


async def run_as(trace: str, run: str, opts: RunOptions, work: Work = done) -> TaskResult[Any]:
    return await with_run(RunIdentity(trace, run), opts, work)


async def hold_thread(db: Db, trace: str, run: str | None = None, *, sandbox: str | None = None) -> None:
    """Starts a run on the thread directly, leaving it running, as a run still in progress elsewhere would."""
    started = await start_on_thread(
        db, StartingRun(KEY, run or trace, trace, "spec-to-pr", "Spec-to-PR", "maintainer", "replace")
    )
    assert started is not None
    if sandbox:
        await db.query("update runs set sandbox_id = %s where run_id = %s", (sandbox, run or trace))


async def run_row(db: Db, run_id: str) -> dict[str, Any]:
    rows = await db.query("select * from runs where run_id = %s", (run_id,))
    assert rows, f"no run {run_id}"
    return rows[0]


async def test_a_root_run_takes_over_and_stops_the_displaced_trace(db: Db) -> None:
    gh, runs = FakeGitHub(), FakeRuns()
    await hold_thread(db, "trace-a", sandbox="sbx-a")
    await hold_thread(db, "trace-a", "child-a", sandbox="sbx-child")

    seen: list[object] = []

    async def work(run: ThreadRun) -> Outcome[Any]:
        seen.append(run.displaced)
        return Outcome("ok", "did it.")

    result = await run_as("trace-b", "trace-b", options(gh, runs, db), work)

    assert result.state == "done"
    assert runs.cancelled == ["trace-a"]
    assert sorted(runs.terminated) == ["sbx-a", "sbx-child"]
    displaced = seen[0]
    assert displaced is not None
    for run_id in ("trace-a", "child-a"):
        row = await run_row(db, run_id)
        assert (row["state"], row["outcome"]) == ("cancelled", "superseded")
    stopped = await read_stream(db, "run:trace-a", 0)
    assert stopped[-1].type == "run.stopped"


@pytest.mark.parametrize(
    ("status", "expected"),
    [("paused", "stopped"), ("pending", "stopped"), ("succeeded", "inactive"), ("missing", "inactive")],
)
async def test_paused_counts_as_active_and_a_missing_run_as_inactive(status: str, expected: str) -> None:
    runs = FakeRuns({"trace-a": status})
    assert await cancel_if_active(runs, "trace-a") == expected


async def test_an_unreadable_run_is_cancelled_anyway() -> None:
    runs = FakeRuns({"trace-a": "unreadable"})
    assert await cancel_if_active(runs, "trace-a") == "stopped"
    assert runs.cancelled == ["trace-a"]


async def test_unconfirmed_cleanup_is_recorded_and_retried_by_the_next_run(db: Db) -> None:
    gh = FakeGitHub()
    await hold_thread(db, "trace-a", sandbox="sbx-a")

    failing = FakeRuns(fail_cancel=True, fail_terminate=True)
    seen: list[ThreadRun] = []

    async def remember(run: ThreadRun) -> Outcome[Any]:
        seen.append(run)
        return Outcome("ok", "did it.")

    await run_as("trace-b", "trace-b", options(gh, failing, db), remember)
    assert seen[0].cleanup.unconfirmed == ("trace-a", "sbx-a")
    assert (await run_row(db, "trace-a"))["cleanup_pending"] == {"task_run": "trace-a", "sandboxes": ["sbx-a"]}

    working = FakeRuns()
    await run_as("trace-c", "trace-c", options(gh, working, db), remember)
    assert seen[1].cleanup.confirmed == ("trace-a", "sbx-a")
    assert seen[1].cleanup.unconfirmed == ()
    assert working.cancelled == ["trace-a"]
    assert working.terminated == ["sbx-a"]
    assert (await run_row(db, "trace-a"))["cleanup_pending"] is None
    assert (await read_stream(db, "run:trace-a", 0))[-1].type == "run.cleaned_up"


async def test_a_child_does_not_start_once_another_trace_owns_the_thread(db: Db) -> None:
    gh, runs = FakeGitHub(), FakeRuns()
    await hold_thread(db, "trace-b")
    called: list[bool] = []

    async def work(_run: ThreadRun) -> Outcome[Any]:
        called.append(True)
        return Outcome("ok", "did it.")

    result = await run_as("trace-a", "child-a", options(gh, runs, db, mode="continue"), work)
    assert (result.state, result.outcome) == ("cancelled", "superseded")
    assert called == []
    assert runs.cancelled == []


async def test_a_child_continues_its_own_trace(db: Db) -> None:
    gh, runs = FakeGitHub(), FakeRuns()
    await hold_thread(db, "trace-a")
    result = await run_as("trace-a", "child-a", options(gh, runs, db, mode="continue"))
    assert result.state == "done"
    assert runs.cancelled == []


async def test_a_reply_resumes_only_the_latest_waiting_trace(db: Db) -> None:
    gh, runs = FakeGitHub(), FakeRuns()

    async def ask(run: ThreadRun) -> Outcome[Any]:
        await run.await_reply()
        return Outcome("asked", "asked 1 question.", waiting="reply")

    await run_as("trace-a", "trace-a", options(gh, runs, db), ask)
    assert (await run_row(db, "trace-a"))["waiting"] == "reply"

    stale = await run_as("trace-x", "trace-x", options(gh, runs, db, mode="reply", reply_to="trace-other"))
    assert (stale.state, stale.outcome) == ("cancelled", "stale_reply")

    resumed = await run_as("trace-b", "trace-b", options(gh, runs, db, mode="reply", reply_to="trace-a"))
    assert resumed.state == "done"

    again = await run_as("trace-c", "trace-c", options(gh, runs, db, mode="reply", reply_to="trace-a"))
    assert (again.state, again.outcome) == ("cancelled", "stale_reply")
    assert runs.cancelled == []


async def test_concurrent_root_starts_leave_exactly_one_owner(db: Db) -> None:
    def starting(trace: str) -> StartingRun:
        return StartingRun(KEY, trace, trace, "triage", "Triage", "maintainer", "replace")

    started = await asyncio.gather(start_on_thread(db, starting("trace-a")), start_on_thread(db, starting("trace-b")))
    assert all(s is not None for s in started)
    displaced = sorted(str(s.displaced) for s in started if s is not None)
    owner = (await db.query("select trace_id from threads where thread = %s", (KEY,)))[0]["trace_id"]
    loser = "trace-a" if owner == "trace-b" else "trace-b"
    assert displaced == ["None", loser]


async def test_a_run_superseded_mid_work_ends_quietly(db: Db) -> None:
    gh, runs = FakeGitHub(), FakeRuns()
    after_takeover: dict[int, tuple[int, str]] = {}

    async def work(run: ThreadRun) -> Outcome[Any]:
        await hold_thread(db, "trace-b")
        after_takeover.update(gh.comments)
        await run.report_progress("still going")
        return Outcome("ok", "did it.")

    result = await run_as("trace-a", "trace-a", options(gh, runs, db), work)
    assert (result.state, result.outcome) == ("cancelled", "superseded")
    assert gh.comments == after_takeover


async def test_a_rejected_trigger_displaces_nothing_and_comments_only_on_an_empty_thread(db: Db) -> None:
    gh, runs = FakeGitHub(permissions={"stranger": "read"}), FakeRuns()

    first = await run_as("trace-r1", "trace-r1", options(gh, runs, db, actor="stranger"))
    assert (first.state, first.outcome) == ("rejected", "permission_denied")
    assert len(gh.bodies(7)) == 1
    assert "did not run" in gh.bodies(7)[0]

    await hold_thread(db, "trace-a")
    second = await run_as("trace-r2", "trace-r2", options(gh, runs, db, actor="stranger"))
    assert second.state == "rejected"
    assert len(gh.bodies(7)) == 1
    assert (await run_row(db, "trace-a"))["state"] == "running"
    assert runs.cancelled == []


async def test_the_status_view_follows_the_run_and_the_working_label(db: Db) -> None:
    gh, runs = FakeGitHub(), FakeRuns()

    async def work(run: ThreadRun) -> Outcome[Any]:
        await run.report_progress("reading the issue")
        assert WORKING_LABEL in gh.labels[7]
        assert "is running" in gh.bodies(7)[0]
        return Outcome("asked", "asked 1 question.", links=(StatusLink("Questions", "https://q"),))

    await run_as("trace-a", "trace-a", options(gh, runs, db), work)
    assert WORKING_LABEL not in gh.labels[7]
    [body] = gh.bodies(7)
    assert "**Triage** finished: asked 1 question." in body
    assert "- [Questions](https://q)" in body


async def test_concurrent_status_syncs_leave_one_comment(db: Db) -> None:
    gh = FakeGitHub()
    await hold_thread(db, "trace-a")
    await asyncio.gather(sync_status(db, gh, REPO, ISSUE), sync_status(db, gh, REPO, ISSUE))
    assert len(gh.bodies(7)) == 1
    assert gh.deleted
    [comment_id] = gh.comments
    saved = await db.query("select status_comment_id from threads where thread = %s", (KEY,))
    assert saved[0]["status_comment_id"] == comment_id


async def test_a_deleted_status_comment_is_recreated(db: Db) -> None:
    gh = FakeGitHub()
    await hold_thread(db, "trace-a")
    await sync_status(db, gh, REPO, ISSUE)
    gh.comments.clear()
    await db.query("update runs set summary = 'changed' where run_id = 'trace-a'")
    await sync_status(db, gh, REPO, ISSUE)
    assert len(gh.bodies(7)) == 1


async def test_expected_failures_and_agent_stops_end_the_run_failed_but_not_the_task(db: Db) -> None:
    gh, runs = FakeGitHub(), FakeRuns()

    async def expected(_run: ThreadRun) -> Outcome[Any]:
        raise ExpectedFailure("the spec changed.", "spec_changed")

    async def agent_stop(_run: ThreadRun) -> Outcome[Any]:
        raise AgentError("triage", "max_turns", "repository output that must stay private")

    first = await run_as("trace-a", "trace-a", options(gh, runs, db), expected)
    assert (first.state, first.outcome, first.summary) == ("failed", "spec_changed", "the spec changed.")
    second = await run_as("trace-b", "trace-b", options(gh, runs, db), agent_stop)
    assert (second.state, second.outcome) == ("failed", "agent_stopped")
    assert "private" not in "".join(gh.bodies(7))


async def test_an_unexpected_error_is_recorded_without_its_message_and_raised(db: Db) -> None:
    gh, runs = FakeGitHub(), FakeRuns()

    async def boom(_run: ThreadRun) -> Outcome[Any]:
        raise RuntimeError("token=secret")

    with pytest.raises(RuntimeError, match="secret"):
        await run_as("trace-a", "trace-a", options(gh, runs, db), boom)
    row = await run_row(db, "trace-a")
    assert (row["state"], row["outcome"]) == ("failed", "error")
    assert "secret" not in "".join(gh.bodies(7))
