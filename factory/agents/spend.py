from __future__ import annotations

from pydantic_ai import RunContext
from pydantic_ai_harness import SpendLimits
from pydantic_ai_harness.spend import BatchSpendStore, Budget

from factory.agents.tools import AgentDeps
from factory.config_model import SpendSettings


def _repository(ctx: RunContext[AgentDeps]) -> str:
    return ctx.deps.repo


def spend_limits(settings: SpendSettings, store: BatchSpendStore) -> SpendLimits[AgentDeps] | None:
    budgets: list[Budget[AgentDeps]] = []
    if settings.agent_run_usd is not None:
        budgets.append(Budget(usd=settings.agent_run_usd, window="run", name="agent_run"))
    if settings.repo_day_usd is not None:
        budgets.append(Budget(usd=settings.repo_day_usd, window="day", scope=_repository, name="repository"))
    if settings.day_usd is not None:
        budgets.append(Budget(usd=settings.day_usd, window="day", name="deployment"))
    return SpendLimits(budgets=budgets, store=store) if budgets else None
