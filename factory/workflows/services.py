"""Dependency composition for tasks.

Tests install fakes. Production integration supplies the agent/sandbox adapters without making their evolving
modules import-time requirements of the orchestration layer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from factory.agents.definition import AgentDefinition, Placement
from factory.agents.providers import model_hosts
from factory.claims import Claim
from factory.config import RepoContext, get_config
from factory.github import PullRequest
from factory.sandbox import OpenedRenderSandbox, Sandbox
from factory.stages import PrReviewDeps, SpecToPrDeps, TriageDeps
from factory.stages.refresh_snapshot import Snapshot


@dataclass(frozen=True)
class WorkflowServices:
    triage: Callable[[Claim], TriageDeps]
    spec_to_pr: Callable[[Claim], SpecToPrDeps]
    pr_review: Callable[[Claim], PrReviewDeps]
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


def effective_placement(definition: AgentDefinition, brokered_model_access: bool) -> Placement:
    """Use sandbox placement only when the deployment explicitly enables its credential broker."""
    return definition.placement if brokered_model_access else "in-task"


def model_egress(definition: AgentDefinition, brokered_model_access: bool) -> tuple[str, ...]:
    """Provider egress belongs only on a sandbox that runs the model loop."""
    return (
        model_hosts((definition.model,))
        if effective_placement(definition, brokered_model_access) == "in-sandbox"
        else ()
    )


def _production_services() -> WorkflowServices:
    """Import evolving agent/sandbox APIs only when a task actually starts."""
    from typing import cast

    from factory.agents import run_agent
    from factory.agents.registry import IMPLEMENTER, PR_REVIEWER, SPEC_WRITER, TRIAGE
    from factory.changeset import publish
    from factory.github import git_data
    from factory.sandbox import BranchRef, PullRef, build_snapshot, open_sandbox, sandbox_client
    from factory.schemas import Implementation, Review, SpecDraft, TriageDecision

    brokered_model_access = get_config().sandbox.brokered_model_access

    def triage(claim: Claim) -> TriageDeps:
        async def open_for_branch(repo: RepoContext, branch: str) -> OpenedRenderSandbox:
            return await open_sandbox(repo, BranchRef(branch))

        async def run_triage(message: str, sandbox: Sandbox) -> TriageDecision:
            output = await run_agent(
                TRIAGE,
                message,
                event_ids=claim.event_ids,
                sandbox=sandbox,
            )
            return cast(TriageDecision, output)

        async def run_writer(message: str, sandbox: Sandbox) -> SpecDraft:
            output = await run_agent(
                SPEC_WRITER,
                message,
                event_ids=claim.event_ids,
                sandbox=sandbox,
            )
            return cast(SpecDraft, output)

        return TriageDeps(open_for_branch, run_triage, run_writer)

    def spec_to_pr(claim: Claim) -> SpecToPrDeps:
        git = git_data(claim.repo)

        async def open_for_branch(repo: RepoContext, branch: str) -> OpenedRenderSandbox:
            return await open_sandbox(
                repo,
                BranchRef(branch),
                model_egress=model_egress(IMPLEMENTER, brokered_model_access),
            )

        async def implement(message: str, sandbox: Sandbox) -> Implementation:
            output = await run_agent(
                IMPLEMENTER,
                message,
                event_ids=claim.event_ids,
                sandbox=sandbox,
                placement=effective_placement(IMPLEMENTER, brokered_model_access),
                timeout_seconds=60 * 60,
            )
            return cast(Implementation, output)

        return SpecToPrDeps(open_for_branch, implement, publish, git, close_git=True)

    def pr_review(claim: Claim) -> PrReviewDeps:
        async def open_for_pull(repo: RepoContext, pull: PullRequest) -> OpenedRenderSandbox:
            return await open_sandbox(
                repo,
                PullRef(
                    pull.number,
                    pull.head_sha,
                    pull.base_ref,
                ),
                model_egress=model_egress(PR_REVIEWER, brokered_model_access),
            )

        async def review(message: str, sandbox: Sandbox) -> Review:
            output = await run_agent(
                PR_REVIEWER,
                message,
                event_ids=claim.event_ids,
                sandbox=sandbox,
                placement=effective_placement(PR_REVIEWER, brokered_model_access),
            )
            return cast(Review, output)

        return PrReviewDeps(open_for_pull, review)

    async def refresh(repo: RepoContext, sha: str) -> Snapshot:
        return await build_snapshot(sandbox_client(), repo, sha)

    return WorkflowServices(triage, spec_to_pr, pr_review, refresh)
