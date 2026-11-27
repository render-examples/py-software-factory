from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, TypedDict

from factory.changeset import Published, PublishInput, Refused
from factory.config import RepoContext, get_config
from factory.format import VerifyResult, pull_request_body, pull_request_title
from factory.github import GitDataClient, GitHubClient, GitIdentity
from factory.runs import ExpectedFailure, Outcome, Run, StatusLink
from factory.sandbox import Sandbox
from factory.schemas import Implementation, SpecToPrInput
from factory.stages.approval import load_approved_spec
from factory.stages.branch import (
    bot_identity,
    branch_name,
    commit_date,
    commit_message,
    owned_commits,
    refusal_summary,
)
from factory.stages.messages import implement_message, merge_implementation, repair_message
from factory.stages.verify import VerifyRun, all_passed, failure_summary, run_verify


class SpecToPrHandoff(TypedDict):
    kind: Literal["pull_request"]
    pr: int
    head_sha: str


class ImplementationSandbox(Sandbox, Protocol):
    @property
    def head_sha(self) -> str: ...


class OpenImplementationSandbox(Protocol):
    async def __call__(self, repo: RepoContext, branch: str) -> ImplementationSandbox: ...


RunImplementer = Callable[[str, ImplementationSandbox], Awaitable[Implementation]]
PublishChanges = Callable[[PublishInput], Awaitable[Published | Refused]]
IdentityFactory = Callable[[GitHubClient, str], Awaitable[GitIdentity]]


@dataclass(frozen=True)
class SpecToPrDeps:
    open_sandbox: OpenImplementationSandbox
    run_implementer: RunImplementer
    publish: PublishChanges
    git: GitDataClient
    identity: IdentityFactory = bot_identity
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    close_git: bool = False


async def spec_to_pr_stage(run: Run, task_input: SpecToPrInput, deps: SpecToPrDeps) -> Outcome[SpecToPrHandoff]:
    gh = run.gh
    approved = await load_approved_spec(run.gh, task_input.issue, task_input.approval)
    issue = await gh.get_issue(task_input.issue)
    if issue.is_pull:
        raise ExpectedFailure("Spec-to-PR runs on issues, not pull requests.", "not_an_issue")

    date = commit_date(deps.now())
    author = await deps.identity(gh, date)
    branch = branch_name(task_input.issue)
    await run.report_progress("opening a sandbox")
    sandbox: ImplementationSandbox | None = None
    try:
        sandbox = await deps.open_sandbox(run.repo, run.repo.config.base_branch)
        await run.track_sandbox(sandbox.id)
        base_sha = sandbox.head_sha
        owned = await owned_commits(deps.git, branch, base_sha, author)
        await run.report_progress("implementing the spec")
        implementation = await deps.run_implementer(
            implement_message(run.repo, issue.number, issue.title, approved.spec), sandbox
        )
        if not await has_changes(sandbox):
            return _no_changes()

        verification: list[VerifyRun] = []
        rounds = get_config().limits.repair_rounds
        for repair_round in range(rounds + 1):
            await run.report_progress("running the verify commands")
            verification = await run_verify(sandbox, run.repo.config.verify)
            if all_passed(verification):
                break
            if repair_round == rounds:
                after = ""
                if rounds:
                    suffix = "" if rounds == 1 else "s"
                    after = f" after {rounds} repair round{suffix}"
                raise ExpectedFailure(
                    f"verification failed{after}: {failure_summary(verification)}. No PR was opened.",
                    "verification_failed",
                )
            round_number = repair_round + 1
            await run.report_progress(f"repairing failed verification (round {round_number} of {rounds})")
            repair = await deps.run_implementer(
                repair_message(
                    run.repo,
                    issue.number,
                    issue.title,
                    approved.spec,
                    verification,
                    round_number,
                    rounds,
                ),
                sandbox,
            )
            implementation = merge_implementation(implementation, repair)

        await run.report_progress("publishing the branch")
        published = await deps.publish(
            PublishInput(
                sandbox=sandbox,
                repo=run.repo,
                base_sha=base_sha,
                branch=branch,
                message=commit_message(approved.spec, task_input.issue),
                author=author,
                git=deps.git,
                owned_commits=owned,
                assert_owned=run.assert_current,
            )
        )
        if isinstance(published, Refused):
            if all(reason.kind == "no_changes" for reason in published.refused):
                return _no_changes()
            raise ExpectedFailure(refusal_summary(published.refused), "publish_refused")

        await run.report_progress("opening the draft pull request")
        pull, _created = await gh.ensure_pull(
            head=branch,
            base=run.repo.config.base_branch,
            title=pull_request_title(approved.spec),
            body=pull_request_body(
                issue=task_input.issue,
                spec=approved.spec,
                implementation=implementation,
                verification=[VerifyResult(check.command, check.passed, check.duration_ms) for check in verification],
                spec_url=approved.comment.html_url,
            ),
        )
        handoff: SpecToPrHandoff = {
            "kind": "pull_request",
            "pr": pull.number,
            "head_sha": pull.head_sha,
        }
        return Outcome(
            "pr_opened",
            f"opened draft PR #{pull.number}. Review it before marking it ready.",
            links=(StatusLink(f"Draft PR #{pull.number}", pull.html_url),),
            data=handoff,
        )
    finally:
        try:
            if sandbox is not None:
                await sandbox.close()
        finally:
            if deps.close_git:
                await deps.git.aclose()


async def has_changes(sandbox: ImplementationSandbox) -> bool:
    result = await sandbox.exec("git status --porcelain --untracked-files=all", timeout_seconds=60, as_runner=True)
    exit_code = result.exit_code
    stderr = str(result.stderr)
    if exit_code != 0:
        raise RuntimeError(f"git status exited {exit_code}: {stderr.strip()[-300:]}")
    return bool(str(result.stdout).strip())


def _no_changes() -> Outcome[SpecToPrHandoff]:
    return Outcome(
        "no_changes",
        "the implementation agent made no changes, so there is nothing to open a PR for.",
    )
