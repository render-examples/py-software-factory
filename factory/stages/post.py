from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal, Protocol

from factory.format import VerifyResult, render_review
from factory.github import (
    GitHubError,
    InlineComment,
    PullRequestReview,
    ReviewMarker,
    parse_marker,
    render_marker,
)
from factory.schemas import Review
from factory.stages.verify import VerifyRun


class ReviewGitHub(Protocol):
    async def bot_login(self) -> str: ...
    async def list_reviews(self, pr: int) -> list[PullRequestReview]: ...
    async def create_review(
        self,
        pr: int,
        *,
        commit_id: str,
        body: str,
        event: Literal["COMMENT"],
        comments: Sequence[InlineComment],
    ) -> PullRequestReview: ...


async def find_review(gh: ReviewGitHub, pr: int, head_sha: str) -> PullRequestReview | None:
    bot = (await gh.bot_login()).lower()
    for review in await gh.list_reviews(pr):
        marker = parse_marker(review.body, ReviewMarker)
        if review.author.login.lower() == bot and marker and marker.head_sha == head_sha:
            return review
    return None


async def post_review(
    gh: ReviewGitHub,
    *,
    pr: int,
    head_sha: str,
    run_id: str,
    review: Review,
    verification: list[VerifyRun],
    is_commentable: Callable[[str, int], bool],
) -> tuple[PullRequestReview, int]:
    marker = render_marker(ReviewMarker(head_sha=head_sha, run_id=run_id))
    formatted_verify = [VerifyResult(run.command, run.passed, run.duration_ms) for run in verification]

    async def post(place_inline: Callable[[str, int], bool]) -> tuple[PullRequestReview, int]:
        payload = render_review(review, verification=formatted_verify, is_commentable=place_inline)
        posted = await gh.create_review(
            pr,
            commit_id=head_sha,
            body=f"{payload.body}\n\n{marker}",
            event="COMMENT",
            comments=payload.comments,
        )
        return posted, len(payload.comments)

    try:
        return await post(is_commentable)
    except GitHubError as error:
        if error.status != 422:
            raise
        payload = render_review(review, verification=formatted_verify, is_commentable=is_commentable)
        if not payload.comments:
            raise

        def nowhere(_path: str, _line: int) -> bool:
            return False

        return await post(nowhere)
