"""Plain Triage stage with all external behavior injected."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from factory.claims import Claim, Outcome, StatusLink
from factory.config import RepoContext
from factory.format import triage_decline_body, triage_questions_body
from factory.github import Issue, IssueComment, QuestionsMarker, render_marker
from factory.schemas import TriageDecision, TriageInput
from factory.stages.spec import SpecSandbox, SpecWriter, TriageHandoff, draft_spec, publish_spec
from factory.stages.thread import read_thread, spec_writer_message, triage_message


class TriageGitHub(Protocol):
    async def get_issue(self, number: int) -> Issue: ...
    async def list_issue_comments(self, number: int) -> list[IssueComment]: ...
    async def bot_login(self) -> str: ...
    async def create_comment(self, number: int, body: str) -> IssueComment: ...
    async def update_comment(self, comment_id: int, body: str) -> IssueComment: ...
    async def remove_label(self, number: int, label: str) -> None: ...


class OpenedSandbox(SpecSandbox, Protocol):
    pass


class OpenTriageSandbox(Protocol):
    async def __call__(self, repo: RepoContext, branch: str) -> OpenedSandbox: ...


class RunTriageAgent(Protocol):
    async def __call__(self, message: str, sandbox: SpecSandbox) -> TriageDecision: ...


@dataclass(frozen=True)
class TriageDeps:
    open_sandbox: OpenTriageSandbox
    run_triage: RunTriageAgent
    run_spec_writer: SpecWriter


async def triage_stage(claim: Claim, task_input: TriageInput, deps: TriageDeps) -> Outcome[TriageHandoff]:
    gh: TriageGitHub = claim.gh
    issue = await gh.get_issue(task_input.issue)
    comments = await gh.list_issue_comments(task_input.issue)
    bot = await gh.bot_login()
    if task_input.comment_id is not None:
        trigger = next((comment for comment in comments if comment.id == task_input.comment_id), None)
        if trigger is None or trigger.author.login.lower() == bot.lower():
            return Outcome(
                "skipped",
                "the comment that started this run is the factory's own or no longer exists.",
            )
    thread = read_thread(issue, comments, bot)

    await claim.report_progress("opening a sandbox")
    sandbox = await deps.open_sandbox(claim.repo, claim.repo.config.base_branch)
    try:
        await claim.track_sandbox(sandbox.id)
        await claim.report_progress("reading the issue and the repository")
        decision = await deps.run_triage(triage_message(claim.repo.full_name, thread), sandbox)
        if decision.decision == "ask":
            await claim.report_progress("posting questions")
            round_number = (thread.questions.round + 1) if thread.questions else 1
            marker = render_marker(QuestionsMarker(round=round_number, questions=decision.questions))
            body = f"{triage_questions_body(decision.questions)}\n\n{marker}"
            comment = (
                await gh.update_comment(thread.questions.comment.id, body)
                if thread.questions
                else await gh.create_comment(issue.number, body)
            )
            count = len(decision.questions)
            return Outcome(
                "asked",
                f"asked {count} question{'' if count == 1 else 's'}.",
                links=(StatusLink("Questions", comment.html_url),),
            )
        if decision.decision == "decline":
            await claim.report_progress("posting the decision")
            comment = await gh.create_comment(issue.number, triage_decline_body(decision.rationale))
            return Outcome(
                "declined",
                "declined the issue.",
                links=(StatusLink("Reason", comment.html_url),),
            )
        await claim.report_progress("revising the spec" if thread.spec else "drafting the spec")
        drafted = await draft_spec(
            deps.run_spec_writer,
            sandbox,
            spec_writer_message(claim.repo.full_name, thread, decision),
        )
        await claim.report_progress("posting the spec")
        return await publish_spec(gh, issue, thread.spec, drafted)
    finally:
        await sandbox.close()
