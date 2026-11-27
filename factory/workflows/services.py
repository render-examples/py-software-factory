"""Dependency composition for tasks.

Tests install fakes. Production integration supplies the agent/sandbox adapters without making their evolving
modules import-time requirements of the orchestration layer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from factory.config import RepoContext
from factory.github import PullRequest
from factory.runs import Run
from factory.sandbox import OpenedRenderSandbox, Sandbox
from factory.stages import PrReviewDeps, SpecToPrDeps, TriageDeps
from factory.stages.refresh_snapshot import Snapshot


@dataclass(frozen=True)
class WorkflowServices:
    triage: Callable[[Run], TriageDeps]
    spec_to_pr: Callable[[Run], SpecToPrDeps]
    pr_review: Callable[[Run], PrReviewDeps]
    build_snapshot: Callable[[RepoContext, str], Awaitable[Snapshot]]


_services: WorkflowServices | None = None


def configure_services(services: WorkflowServices | None) -> None:
    """Install adapters at service startup, or fake adapters in tests."""
    global _services
    _services = services


def get_services() -> WorkflowServices:
    if _services is None:
        return _production_services()
    return _services


def _production_services() -> WorkflowServices:
    """Import evolving agent/sandbox APIs only when a task actually starts."""
    from typing import cast

    from factory.agents import run_agent
    from factory.agents.registry import IMPLEMENTER, PR_REVIEWER, SPEC_WRITER, TRIAGE
    from factory.changeset import publish
    from factory.github import git_data
    from factory.sandbox import BranchRef, PullRef, build_snapshot, open_sandbox, sandbox_client
    from factory.schemas import Implementation, Review, SpecDraft, TriageDecision

    async def open_for_branch(repo: RepoContext, branch: str) -> OpenedRenderSandbox:
        return await open_sandbox(repo, BranchRef(branch))

    def triage(run: Run) -> TriageDeps:
        async def run_triage(message: str, sandbox: Sandbox) -> TriageDecision:
            return cast(TriageDecision, await run_agent(TRIAGE, message, event_ids=run.event_ids, sandbox=sandbox))

        async def run_writer(message: str, sandbox: Sandbox) -> SpecDraft:
            return cast(SpecDraft, await run_agent(SPEC_WRITER, message, event_ids=run.event_ids, sandbox=sandbox))

        return TriageDeps(open_for_branch, run_triage, run_writer)

    def spec_to_pr(run: Run) -> SpecToPrDeps:
        async def implement(message: str, sandbox: Sandbox) -> Implementation:
            output = await run_agent(
                IMPLEMENTER, message, event_ids=run.event_ids, sandbox=sandbox, timeout_seconds=60 * 60
            )
            return cast(Implementation, output)

        return SpecToPrDeps(open_for_branch, implement, publish, git_data(run.repo), close_git=True)

    def pr_review(run: Run) -> PrReviewDeps:
        async def open_for_pull(repo: RepoContext, pull: PullRequest) -> OpenedRenderSandbox:
            return await open_sandbox(repo, PullRef(pull.number, pull.head_sha, pull.base_ref))

        async def review(message: str, sandbox: Sandbox) -> Review:
            return cast(Review, await run_agent(PR_REVIEWER, message, event_ids=run.event_ids, sandbox=sandbox))

        return PrReviewDeps(open_for_pull, review)

    async def refresh(repo: RepoContext, sha: str) -> Snapshot:
        return await build_snapshot(sandbox_client(), repo, sha)

    return WorkflowServices(triage, spec_to_pr, pr_review, refresh)
