from __future__ import annotations

from factory.claims import Subject
from factory.config import Tasks
from factory.schemas import PrReviewInput
from factory.stages import pr_review_stage
from factory.workflows.github_task import GitHubTaskSpec, define_github_task
from factory.workflows.services import get_services


def _subject(task_input: PrReviewInput, child: bool) -> Subject:
    if task_input.autopilot_issue is not None and not child:
        raise ValueError("autopilot_issue is valid only in a parent workflow")
    return (
        Subject("issue", task_input.autopilot_issue)
        if task_input.autopilot_issue is not None
        else Subject("pr", task_input.pr)
    )


pr_review = define_github_task(
    GitHubTaskSpec(
        name=Tasks.PR_REVIEW,
        title="PR Reviewer",
        input_type=PrReviewInput,
        subject=_subject,
        timeout_seconds=2 * 60 * 60,
    ),
    lambda claim, task_input, _ctx: pr_review_stage(claim, task_input, get_services().pr_review(claim)),
)

pr_review_task = pr_review.task
run_pr_review = pr_review.run
