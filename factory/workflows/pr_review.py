from __future__ import annotations

from factory.claims import Subject
from factory.config import Tasks
from factory.schemas import PrReviewInput
from factory.stages import pr_review_stage
from factory.workflows.github_task import GitHubTaskSpec, define_github_task
from factory.workflows.services import get_services


def _subject(input: PrReviewInput, child: bool) -> Subject:
    if input.autopilot_issue is not None and not child:
        raise ValueError("autopilot_issue is valid only in a parent workflow")
    return Subject("issue", input.autopilot_issue) if input.autopilot_issue is not None else Subject("pr", input.pr)


pr_review = define_github_task(
    GitHubTaskSpec(
        name=Tasks.PR_REVIEW,
        title="PR Reviewer",
        input_type=PrReviewInput,
        subject=_subject,
        timeout_seconds=2 * 60 * 60,
    ),
    lambda claim, input, _ctx: pr_review_stage(claim, input, get_services().pr_review(claim)),
)

pr_review_task = pr_review.task
run_pr_review = pr_review.run
