from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

import pytest

from factory.agents import AgentError
from factory.claims import (
    WORKING_LABEL,
    Claim,
    ClaimOptions,
    ExpectedFailure,
    Outcome,
    RunIdentity,
    RunNotFoundError,
    StatusUpdate,
    Subject,
    finish_status,
    read_status,
    take_over,
    with_claim,
)
from factory.config import RepoContext
from factory.github import (
    Actor,
    GitHubClient,
    IssueComment,
    Permission,
    StatusMarker,
    parse_marker,
    render_marker,
)
from tests.fixtures import REPO as REPO_NAME
from tests.fixtures import TEST_CONFIG

BOT = "software-factory[bot]"
REPO = RepoContext("acme", "widget", REPO_NAME, 5, TEST_CONFIG.repos[REPO_NAME])
SUBJECT = Subject("issue", 12)


class FakeGitHub:
    def __init__(self) -> None:
        self.comments: list[IssueComment] = []
        self.labels: set[str] = set()
        self.permissions: dict[str, Permission] = {}
        self._next_id = 100

    async def bot_login(self) -> str:
        return BOT

    async def list_issue_comments(self, number: int) -> list[IssueComment]:
        return list(self.comments)

    async def create_comment(self, number: int, body: str) -> IssueComment:
        return self.add(BOT, body)

    async def update_comment(self, comment_id: int, body: str) -> IssueComment:
        for i, c in enumerate(self.comments):
            if c.id == comment_id:
                self.comments[i] = IssueComment(c.id, body, c.author, c.created_at, c.updated_at, c.html_url)
                return self.comments[i]
        raise KeyError(comment_id)

    async def ensure_label(self, name: str, color: str, description: str = "") -> None:
        return None

    async def add_labels(self, number: int, labels: Sequence[str]) -> None:
        self.labels.update(labels)

    async def remove_label(self, number: int, label: str) -> None:
        self.labels.discard(label)

    async def get_permission(self, login: str) -> Permission:
        return self.permissions.get(login, "none")

    async def aclose(self) -> None:
        return None

    def add(self, login: str, body: str) -> IssueComment:
        kind = "Bot" if login.endswith("[bot]") else "User"
        comment = IssueComment(self._next_id, body, Actor(login, kind), "2026-01-01T00:00:00Z", "", "")
        self._next_id += 1
        self.comments.append(comment)
        return comment

    def add_status(
        self,
        run_id: str,
        state: str = "running",
        sandbox_id: str | None = None,
        trace_id: str | None = None,
        root_stage: str | None = None,
    ) -> IssueComment:
        marker = StatusMarker(
            run_id=run_id,
            trace_id=trace_id,
            root_stage=root_stage,
            stage="triage",
            state=state,  # pyright: ignore[reportArgumentType]
            sandbox_id=sandbox_id,
        )
        return self.add(BOT, f"status\n\n{render_marker(marker)}")

    def status(self) -> StatusMarker | None:
        return parse_marker(self.comments[0].body, StatusMarker) if self.comments else None

    def client(self) -> GitHubClient:
        return cast(GitHubClient, self)


class FakeRuns:
    def __init__(self, statuses: dict[str, str] | None = None) -> None:
        self.statuses = statuses or {}
        self.reads: list[str] = []
        self.cancelled: list[str] = []
        self.terminated: list[str] = []

    async def get_status(self, run_id: str) -> str:
        self.reads.append(run_id)
        if run_id not in self.statuses:
            raise RunNotFoundError(run_id)
        return self.statuses[run_id]

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)
        self.statuses[run_id] = "canceled"

    async def terminate_sandbox(self, sandbox_id: str) -> None:
        self.terminated.append(sandbox_id)


def events(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]


def identity(run_id: str, trace_id: str | None = None) -> RunIdentity:
    return RunIdentity(trace_id=trace_id or run_id, run_id=run_id)


class TestTakeOver:
    @pytest.mark.parametrize("render_status", ["pending", "running", "paused"])
    async def test_cancels_every_active_render_task_status(self, render_status: str) -> None:
        gh = FakeGitHub()
        gh.add_status("trn-old", "running")
        runs = FakeRuns({"trn-old": render_status})
        result = await take_over(
            gh=gh,
            repo=REPO.full_name,
            subject=SUBJECT,
            task="triage",
            title="Triage",
            identity=identity("trn-new"),
            runs=runs,
        )
        assert result.cancelled_previous
        assert runs.cancelled == ["trn-old"]

    async def test_cancels_an_active_previous_run_terminates_its_sandbox_and_reuses_the_comment(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gh = FakeGitHub()
        existing = gh.add_status("trn-old", "running", "sbx-old")
        runs = FakeRuns({"trn-old": "running"})
        result = await take_over(
            gh=gh,
            repo=REPO.full_name,
            subject=SUBJECT,
            task="triage",
            title="Triage",
            identity=identity("trn-new"),
            runs=runs,
        )
        assert runs.cancelled == ["trn-old"]
        assert runs.terminated == ["sbx-old"]
        # The stopped run is killed before it can record how it ended, so the takeover records it.
        assert {
            "level": "info",
            "event": "run.finished",
            "trace_id": "trn-old",
            "run_id": "trn-old",
            "repo": REPO.full_name,
            "subject": "issue#12",
            "stage": "triage",
            "state": "cancelled",
            "outcome": "superseded",
            "stopped_by": "trn-new",
        } in events(capsys)
        assert (result.acquired, result.comment_id, result.previous_run_id, result.cancelled_previous) == (
            True,
            existing.id,
            "trn-old",
            True,
        )
        assert len(gh.comments) == 1
        status = gh.status()
        assert status is not None
        assert (status.run_id, status.state, status.stage) == ("trn-new", "running", "triage")
        assert "is running (run `trn-new`)" in gh.comments[0].body
        assert WORKING_LABEL in gh.labels

    async def test_cancels_the_previous_root_when_the_recorded_task_was_its_child(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gh = FakeGitHub()
        gh.add_status("trn-child-old", "running", "sbx-old", "trn-root-old")
        runs = FakeRuns({"trn-root-old": "running"})
        await take_over(
            gh=gh,
            repo=REPO.full_name,
            subject=SUBJECT,
            task="triage",
            title="Triage",
            identity=identity("trn-child-new", "trn-root-new"),
            runs=runs,
        )
        assert runs.cancelled == ["trn-root-old"]
        assert any(
            e["trace_id"] == "trn-root-old" and e["run_id"] == "trn-child-old" and e["outcome"] == "superseded"
            for e in events(capsys)
        )

    async def test_moves_to_another_child_of_the_same_root_without_cancelling_the_parent(self) -> None:
        gh = FakeGitHub()
        gh.add_status("trn-triage", "done", None, "trn-root", "autopilot")
        runs = FakeRuns({"trn-root": "running"})
        await take_over(
            gh=gh,
            repo=REPO.full_name,
            subject=SUBJECT,
            task="spec-to-pr",
            title="Spec-to-PR",
            identity=identity("trn-spec", "trn-root"),
            runs=runs,
        )
        assert runs.reads == []
        assert runs.cancelled == []
        status = gh.status()
        assert status is not None
        assert (status.trace_id, status.root_stage, status.run_id, status.stage) == (
            "trn-root",
            "autopilot",
            "trn-spec",
            "spec-to-pr",
        )

    async def test_refuses_a_late_child_instead_of_taking_over_from_a_newer_root(self) -> None:
        gh = FakeGitHub()
        existing = gh.add_status("trn-new-child", "running", None, "trn-new-root")
        runs = FakeRuns({"trn-new-root": "running"})
        result = await take_over(
            gh=gh,
            repo=REPO.full_name,
            subject=SUBJECT,
            task="spec-to-pr",
            title="Spec-to-PR",
            identity=identity("trn-late-child", "trn-old-root"),
            runs=runs,
            mode="continue",
        )
        assert (result.acquired, result.comment_id, result.previous_run_id, result.cancelled_previous) == (
            False,
            existing.id,
            "trn-new-child",
            False,
        )
        assert runs.reads == []
        status = gh.status()
        assert status is not None
        assert (status.trace_id, status.run_id) == ("trn-new-root", "trn-new-child")

    async def test_fails_closed_when_the_previous_runs_state_is_unknown(self) -> None:
        class BrokenRuns(FakeRuns):
            async def get_status(self, run_id: str) -> str:
                raise RuntimeError("render down")

        gh = FakeGitHub()
        gh.add_status("trn-old", "running", "sbx-old")
        with pytest.raises(RuntimeError, match="render down"):
            await take_over(
                gh=gh,
                repo=REPO.full_name,
                subject=SUBJECT,
                task="triage",
                title="Triage",
                identity=identity("trn-new"),
                runs=BrokenRuns(),
            )
        status = gh.status()
        assert status is not None
        assert status.run_id == "trn-old"


class TestStatus:
    async def test_never_overwrites_a_newer_runs_status(self) -> None:
        gh = FakeGitHub()
        gh.add_status("trn-new")
        gh.labels.add(WORKING_LABEL)
        update = StatusUpdate(12, identity("trn-old"), "triage", "Triage", "failed", "x")
        assert await finish_status(gh, update) is False
        status = gh.status()
        assert status is not None
        assert status.run_id == "trn-new"
        assert WORKING_LABEL in gh.labels

    async def test_keeps_the_working_label_between_child_stages_and_removes_it_when_the_root_finishes(self) -> None:
        gh = FakeGitHub()
        gh.add_status("trn-child", "running", None, "trn-root")
        gh.labels.add(WORKING_LABEL)
        await finish_status(gh, StatusUpdate(12, identity("trn-child", "trn-root"), "triage", "Triage", "done", "done"))
        assert WORKING_LABEL in gh.labels
        await finish_status(gh, StatusUpdate(12, identity("trn-root"), "autopilot", "Autopilot", "done", "complete"))
        assert WORKING_LABEL not in gh.labels


class TestWithClaim:
    ID = identity("trn-run")

    def opts(self, gh: FakeGitHub, actor: str, runs: FakeRuns | None = None) -> ClaimOptions:
        return ClaimOptions(
            repo=REPO,
            subject=SUBJECT,
            task="triage",
            title="Triage",
            actor=actor,
            runs=runs or FakeRuns(),
            gh=gh.client(),
        )

    async def test_rejects_an_actor_without_permission_before_taking_over(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gh = FakeGitHub()
        gh.permissions = {"reader": "read"}
        called: list[bool] = []

        async def work(_: Claim) -> Outcome[None]:
            called.append(True)
            return Outcome("x", "x")

        result = await with_claim(self.ID, self.opts(gh, "reader"), work)
        assert called == []
        assert (result.state, result.outcome, result.trace_id, result.run_id) == (
            "rejected",
            "permission_denied",
            "trn-run",
            "trn-run",
        )
        assert "did not run: @reader needs `write` permission" in gh.comments[0].body
        status = gh.status()
        assert status is not None
        assert status.state == "rejected"
        assert WORKING_LABEL not in gh.labels
        [event] = events(capsys)
        assert (event["event"], event["trace_id"], event["state"]) == ("run.finished", "trn-run", "rejected")

    async def test_checks_the_actors_current_permission_on_every_run(self) -> None:
        gh = FakeGitHub()
        gh.permissions = {"m": "write"}

        async def work(_: Claim) -> Outcome[None]:
            return Outcome("ok", "ok")

        assert (await with_claim(self.ID, self.opts(gh, "m"), work)).state == "done"
        gh.permissions = {"m": "read"}
        assert (await with_claim(identity("trn-2"), self.opts(gh, "m"), work)).state == "rejected"

    async def test_runs_the_work_and_records_events_with_the_trace_id_and_outcome(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gh = FakeGitHub()
        gh.permissions = {"maintainer": "maintain"}
        gh.add_status("trn-prev")
        runs = FakeRuns({"trn-prev": "running"})

        async def work(claim: Claim) -> Outcome[None]:
            assert claim.run_id == "trn-run"
            assert WORKING_LABEL in gh.labels
            await claim.track_sandbox("sbx-run")
            status = gh.status()
            assert status is not None
            assert (status.run_id, status.state, status.sandbox_id) == ("trn-run", "running", "sbx-run")
            await claim.report_progress("reading the issue")
            assert "reading the issue" in gh.comments[0].body
            status = gh.status()
            assert status is not None
            assert status.sandbox_id == "sbx-run"
            return Outcome("asked-questions", "asked 2 questions")

        result = await with_claim(self.ID, self.opts(gh, "maintainer", runs), work)
        assert runs.cancelled == ["trn-prev"]
        status = gh.status()
        assert status is not None
        assert status.sandbox_id is None
        assert (result.state, result.outcome, result.summary) == ("done", "asked-questions", "asked 2 questions")
        assert len(gh.comments) == 1
        assert "**Triage** finished: asked 2 questions" in gh.comments[0].body
        assert WORKING_LABEL not in gh.labels
        recorded = events(capsys)
        assert [(e["trace_id"], e["state"], e["outcome"]) for e in recorded] == [
            ("trn-prev", "cancelled", "superseded"),
            ("trn-run", "done", "asked-questions"),
        ]
        assert recorded[1]["repo"] == REPO.full_name
        assert recorded[1]["subject"] == "issue#12"

    async def test_stops_at_a_checkpoint_after_a_newer_run_claims_the_subject(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gh = FakeGitHub()
        gh.permissions = {"w": "write"}

        async def work(claim: Claim) -> Outcome[None]:
            gh.add_status("trn-new")
            await claim.report_progress("doing expensive work")
            return Outcome("done", "done")

        result = await with_claim(self.ID, self.opts(gh, "w"), work)
        assert (result.state, result.outcome) == ("cancelled", "superseded")
        current = await read_status(gh, 12)
        assert current is not None
        assert current[1].run_id == "trn-new"
        assert events(capsys)[-1]["outcome"] == "superseded"

    async def test_publishes_an_expected_failure_and_succeeds(self) -> None:
        gh = FakeGitHub()
        gh.permissions = {"w": "write"}

        async def work(_: Claim) -> Outcome[None]:
            raise ExpectedFailure("the spec changed after it was approved.", "spec_changed")

        result = await with_claim(self.ID, self.opts(gh, "w"), work)
        assert (result.state, result.outcome) == ("failed", "spec_changed")
        assert "**Triage** failed: the spec changed after it was approved." in gh.comments[0].body

    async def test_records_an_unexpected_error_without_leaking_it_removes_the_label_and_reraises(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gh = FakeGitHub()
        gh.permissions = {"w": "write"}
        boom = RuntimeError("connect ECONNREFUSED token=ghs_secret")

        async def work(_: Claim) -> Outcome[None]:
            raise boom

        with pytest.raises(RuntimeError) as caught:
            await with_claim(self.ID, self.opts(gh, "w"), work)
        assert caught.value is boom
        body = gh.comments[0].body
        assert "**Triage** failed: something went wrong" in body
        assert "ghs_secret" not in body
        assert "ECONNREFUSED" not in body
        status = gh.status()
        assert status is not None
        assert status.state == "failed"
        assert WORKING_LABEL not in gh.labels
        last = events(capsys)[-1]
        assert (last["state"], last["outcome"]) == ("failed", "error")

    async def test_reports_a_model_side_agent_stop_but_fails_the_run_for_a_platform_side_one(self) -> None:
        async def run(error: Exception) -> tuple[FakeGitHub, Any]:
            gh = FakeGitHub()
            gh.permissions = {"w": "write"}

            async def work(_: Claim) -> Outcome[None]:
                raise error

            return gh, await with_claim(self.ID, self.opts(gh, "w"), work)

        gh, result = await run(AgentError("triage", "max_turns", "turn limit (40) after reading /secret/path"))
        assert (result.state, result.outcome) == ("failed", "agent_stopped")
        assert "the triage agent ran out of turns" in gh.comments[0].body
        assert "/secret/path" not in gh.comments[0].body

        lost = AgentError("triage", "runner_lost", "the runner ended without a result")
        with pytest.raises(AgentError) as caught:
            await run(lost)
        assert caught.value is lost
