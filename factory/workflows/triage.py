from __future__ import annotations

from factory.config import Tasks, get_config
from factory.schemas import TriageInput
from factory.stages import triage_stage
from factory.threads import GitHubThread
from factory.workflows.github_task import GitHubTaskSpec, Trigger, define_github_task
from factory.workflows.services import get_services

triage = define_github_task(
    GitHubTaskSpec(
        name=Tasks.TRIAGE,
        title="Triage",
        input_type=TriageInput,
        thread=lambda task_input: GitHubThread("issue", task_input.issue),
        reply_to=lambda task_input: task_input.reply_to,
        trigger=lambda task_input: (
            Trigger(get_config().labels.triage, task_input.issue) if task_input.comment_id is None else None
        ),
        timeout_seconds=60 * 60,
    ),
    lambda run, task_input, _ctx: triage_stage(run, task_input, get_services().triage(run)),
)

triage_task = triage.task
run_triage = triage.run
