from __future__ import annotations

from factory.claims import Subject
from factory.config import Tasks
from factory.schemas import SpecToPrInput
from factory.stages import spec_to_pr_stage
from factory.workflows.github_task import GitHubTaskSpec, define_github_task
from factory.workflows.services import get_services

spec_to_pr = define_github_task(
    GitHubTaskSpec(
        name=Tasks.SPEC_TO_PR,
        title="Spec-to-PR",
        input_type=SpecToPrInput,
        subject=lambda task_input, _child: Subject("issue", task_input.issue),
        timeout_seconds=4 * 60 * 60,
    ),
    lambda claim, task_input, _ctx: spec_to_pr_stage(claim, task_input, get_services().spec_to_pr(claim)),
)

spec_to_pr_task = spec_to_pr.task
run_spec_to_pr = spec_to_pr.run
