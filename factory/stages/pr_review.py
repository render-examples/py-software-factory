"""Pull-request review stage; always posts a COMMENT review."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from factory.claims import Claim, Outcome, StatusLink
from factory.config import RepoContext
from factory.github import PullFile, PullRequest
from factory.sandbox import Sandbox
from factory.sandbox.errors import SandboxHeadMovedError
from factory.schemas import PrReviewInput, Review
from factory.stages.checkout import head_moved, merge_base
from factory.stages.diff_lines import commentable, pull_diff_lines
from factory.stages.post import ReviewGitHub, find_review, post_review
from factory.stages.review_messages import reviewer_message
from factory.stages.verify import VerifySandbox, run_verify


class ReviewSandbox(VerifySandbox, Sandbox, Protocol):
    pass


class ReviewStageGitHub(ReviewGitHub, Protocol):
    async def get_pull(self, number: int) -> PullRequest: ...
    async def list_pull_files(self, number: int) -> list[PullFile]: ...
    async def get_pull_diff(self, number: int) -> str: ...


class OpenReviewSandbox(Protocol):
    async def __call__(self, repo: RepoContext, pull: PullRequest) -> ReviewSandbox: ...


RunReviewer = Callable[[str, ReviewSandbox], Awaitable[Review]]


@dataclass(frozen=True)
class PrReviewDeps:
    open_sandbox: OpenReviewSandbox
    run_reviewer: RunReviewer


async def pr_review_stage(claim: Claim, task_input: PrReviewInput, deps: PrReviewDeps) -> Outcome[None]:
    gh: ReviewStageGitHub = claim.gh
    pull = await gh.get_pull(task_input.pr)
    if pull.state != "open" or pull.merged:
        return Outcome("pr_closed", "the pull request is closed, so there's nothing to review.")
    if pull.head_sha != task_input.head_sha:
        raise head_moved()
    existing = await find_review(gh, task_input.pr, task_input.head_sha)
    if existing:
        return Outcome(
            "already_reviewed",
            "this commit already has a factory review.",
            links=(StatusLink("Review", existing.html_url),),
        )
    files = await gh.list_pull_files(task_input.pr)
    diff_lines = await pull_diff_lines(gh, task_input.pr, files)

    await claim.report_progress("opening a sandbox")
    try:
        sandbox = await deps.open_sandbox(claim.repo, pull)
    except SandboxHeadMovedError as error:
        raise head_moved() from error
    try:
        await claim.track_sandbox(sandbox.id)
        base_ref = await merge_base(sandbox, pull.base_ref)
        await claim.report_progress("running the verify commands")
        verification = await run_verify(sandbox, claim.repo.config.verify)
        await claim.report_progress("reviewing the pull request")
        review = await deps.run_reviewer(
            reviewer_message(claim.repo.full_name, pull, base_ref, files, verification),
            sandbox,
        )
    finally:
        await sandbox.close()

    # Recheck immediately before the durable write. The trigger commit remains the only allowed target.
    if (await gh.get_pull(task_input.pr)).head_sha != task_input.head_sha:
        raise head_moved()
    await claim.report_progress("posting the review")
    posted, inline_count = await post_review(
        gh,
        pr=task_input.pr,
        head_sha=task_input.head_sha,
        run_id=claim.run_id,
        review=review,
        verification=verification,
        is_commentable=commentable(diff_lines),
    )
    failed = sum(not run.passed for run in verification)
    notes = [
        f"risk **{review.risk_tier}**",
        f"{inline_count} inline comment{'' if inline_count == 1 else 's'}",
    ]
    if failed:
        notes.append(f"{failed} of {len(verification)} verify commands failed")
    return Outcome(
        "reviewed",
        f"posted a COMMENT review: {', '.join(notes)}.",
        links=(StatusLink("Review", posted.html_url),),
    )
