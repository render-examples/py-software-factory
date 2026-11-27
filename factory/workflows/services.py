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
    from typing import Any, cast

    from pydantic import BaseModel
    from pydantic_ai_harness.spend import PostgresSpendStore

    from factory.agents import AgentDefinition, run_agent
    from factory.agents.registry import IMPLEMENTER, PR_REVIEWER, SPEC_WRITER, TRIAGE
    from factory.agents.spend import spend_limits
    from factory.changeset import publish
    from factory.config import get_config
    from factory.db import workflows_db
    from factory.github import git_data
    from factory.sandbox import BranchRef, PullRef, build_snapshot, open_sandbox, sandbox_client
    from factory.schemas import Implementation, Review, SpecDraft, TriageDecision

    stores: dict[int, PostgresSpendStore] = {}

    async def agent(
        definition: AgentDefinition, run: Run, message: str, sandbox: Sandbox, timeout_seconds: float = 30 * 60
    ) -> BaseModel:
        """Runs an agent with the configured spend budgets, counted in py-factory-db on the Workflows pool."""
        pool = await workflows_db().pool()
        # The store takes an asyncpg pool; asyncpg's stubs and its protocol disagree on acquire()'s return type.
        store = stores.setdefault(id(pool), PostgresSpendStore(cast(Any, pool)))
        return await run_agent(
            definition,
            message,
            event_ids=run.event_ids,
            sandbox=sandbox,
            timeout_seconds=timeout_seconds,
            spend=spend_limits(get_config().spend, store),
        )

    async def open_for_branch(repo: RepoContext, branch: str) -> OpenedRenderSandbox:
        return await open_sandbox(repo, BranchRef(branch))

    def triage(run: Run) -> TriageDeps:
        async def run_triage(message: str, sandbox: Sandbox) -> TriageDecision:
            return cast(TriageDecision, await agent(TRIAGE, run, message, sandbox))

        async def run_writer(message: str, sandbox: Sandbox) -> SpecDraft:
            return cast(SpecDraft, await agent(SPEC_WRITER, run, message, sandbox))

        return TriageDeps(open_for_branch, run_triage, run_writer)

    def spec_to_pr(run: Run) -> SpecToPrDeps:
        async def implement(message: str, sandbox: Sandbox) -> Implementation:
            return cast(Implementation, await agent(IMPLEMENTER, run, message, sandbox, timeout_seconds=60 * 60))

        return SpecToPrDeps(open_for_branch, implement, publish, git_data(run.repo), close_git=True)

    def pr_review(run: Run) -> PrReviewDeps:
        async def open_for_pull(repo: RepoContext, pull: PullRequest) -> OpenedRenderSandbox:
            return await open_sandbox(repo, PullRef(pull.number, pull.head_sha, pull.base_ref))

        async def review(message: str, sandbox: Sandbox) -> Review:
            return cast(Review, await agent(PR_REVIEWER, run, message, sandbox))

        return PrReviewDeps(open_for_pull, review)

    async def refresh(repo: RepoContext, sha: str) -> Snapshot:
        return await build_snapshot(sandbox_client(), repo, sha)

    return WorkflowServices(triage, spec_to_pr, pr_review, refresh)
