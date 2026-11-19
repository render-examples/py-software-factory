from __future__ import annotations

from factory.config import Tasks, get_config
from factory.schemas import SpecToPrInput
from factory.stages import spec_to_pr_stage
from factory.threads import GitHubThread
from factory.workflows.github_task import GitHubTaskSpec, Trigger, define_github_task
from factory.workflows.services import get_services

spec_to_pr = define_github_task(
    GitHubTaskSpec(
        name=Tasks.SPEC_TO_PR,
        title="Spec-to-PR",
        input_type=SpecToPrInput,
        thread=lambda task_input: GitHubThread("issue", task_input.issue),
        trigger=lambda task_input: Trigger(get_config().labels.spec_approved, task_input.issue),
        timeout_seconds=4 * 60 * 60,
    ),
    lambda run, task_input, _ctx: spec_to_pr_stage(run, task_input, get_services().spec_to_pr(run)),
)

spec_to_pr_task = spec_to_pr.task
run_spec_to_pr = spec_to_pr.run
