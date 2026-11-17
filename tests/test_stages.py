from __future__ import annotations

from typing import Any

import pytest

from factory.claims import ExpectedFailure
from factory.config import require_repo
from factory.github import GitCommit, GitIdentity, PullRequest
from factory.sandbox import ExecResult
from factory.schemas import PrReviewInput
from factory.stages import parse_diff_lines, pr_review_stage, run_verify
from factory.stages.branch import owned_commits
from factory.stages.pr_review import PrReviewDeps
from tests.fixtures import REPO

SHA = "a" * 40


class VerifySandbox:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def exec(self, command: str, *, timeout_seconds: float | None = None, as_runner: bool = False) -> ExecResult:
        self.commands.append(command)
        return ExecResult(1 if command == "second" else 0, "", "failed" if command == "second" else "")


async def test_verify_runs_in_order_and_records_failures() -> None:
    sandbox = VerifySandbox()
    runs = await run_verify(sandbox, ("first", "second", "third"))
    assert sandbox.commands == ["first", "second", "third"]
    assert [run.passed for run in runs] == [True, False, True]
    assert runs[1].output == "failed"


def test_diff_lines_include_context_and_added_right_side_lines() -> None:
    lines = parse_diff_lines(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -2,3 +2,4 @@\n context\n-old\n+new\n+extra\n end\n"
    )
    assert lines == {"a.py": {2, 3, 4, 5}}


class ReviewGitHub:
    async def get_pull(self, number: int) -> PullRequest:
        return PullRequest(
            number=number,
            state="open",
            merged=False,
            draft=True,
            title="title",
            body="",
            head_sha="b" * 40,
            head_ref="feature",
            base_ref="main",
            html_url="url",
        )


class ReviewClaim:
    def __init__(self) -> None:
        self.gh = ReviewGitHub()
        self.repo = require_repo(REPO, 1)
        self.run_id = "run"


async def _never_open(_repo: object, _pull: object) -> Any:
    raise AssertionError("stale heads must stop before opening a sandbox")


async def _never_review(_message: str, _sandbox: object) -> Any:
    raise AssertionError("stale heads must stop before running an agent")


async def test_pr_review_refuses_a_stale_labeled_head() -> None:
    input = PrReviewInput(repo=REPO, installation_id=1, pr=7, head_sha=SHA, actor="maintainer")
    with pytest.raises(ExpectedFailure) as caught:
        await pr_review_stage(
            ReviewClaim(),  # type: ignore[arg-type]
            input,
            PrReviewDeps(_never_open, _never_review),  # type: ignore[arg-type]
        )
    assert caught.value.outcome == "head_moved"


async def test_branch_reuse_requires_the_factory_committer_identity() -> None:
    identity = GitIdentity("factory[bot]", "7+factory[bot]@users.noreply.github.com")

    class Git:
        def __init__(self, committer: GitIdentity) -> None:
            self.committer = committer

        async def get_ref(self, branch: str) -> str | None:
            return SHA

        async def get_commit(self, sha: str) -> GitCommit:
            return GitCommit(sha, "b" * 40, ("c" * 40,), self.committer)

    assert await owned_commits(Git(identity), "factory/issue-7", "d" * 40, identity) == [SHA]
    with pytest.raises(ExpectedFailure, match="didn't make"):
        await owned_commits(
            Git(GitIdentity("person", "person@example.com")),
            "factory/issue-7",
            "d" * 40,
            identity,
        )
