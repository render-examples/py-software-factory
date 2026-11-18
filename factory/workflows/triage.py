from __future__ import annotations

from factory.claims import Subject
from factory.config import Tasks
from factory.schemas import TriageInput
from factory.stages import triage_stage
from factory.workflows.github_task import GitHubTaskSpec, define_github_task
from factory.workflows.services import get_services

triage = define_github_task(
    GitHubTaskSpec(
        name=Tasks.TRIAGE,
        title="Triage",
        input_type=TriageInput,
        subject=lambda task_input, _child: Subject("issue", task_input.issue),
        timeout_seconds=60 * 60,
    ),
    lambda claim, task_input, _ctx: triage_stage(claim, task_input, get_services().triage(claim)),
)

triage_task = triage.task
run_triage = triage.run
