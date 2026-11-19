from __future__ import annotations

from factory.config import Tasks, get_config
from factory.schemas import PrReviewInput
from factory.stages import pr_review_stage
from factory.threads import GitHubThread
from factory.workflows.github_task import GitHubTaskSpec, Trigger, define_github_task
from factory.workflows.services import get_services

pr_review = define_github_task(
    GitHubTaskSpec(
        name=Tasks.PR_REVIEW,
        title="PR Reviewer",
        input_type=PrReviewInput,
        thread=lambda task_input: GitHubThread("pr", task_input.pr),
        trigger=lambda task_input: Trigger(get_config().labels.pr_review, task_input.pr),
        timeout_seconds=2 * 60 * 60,
    ),
    lambda run, task_input, _ctx: pr_review_stage(run, task_input, get_services().pr_review(run)),
)

pr_review_task = pr_review.task
run_pr_review = pr_review.run
