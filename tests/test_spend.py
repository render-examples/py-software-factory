from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_harness import SpendLimits
from pydantic_ai_harness.spend import Budget, InMemorySpendStore, PostgresSpendStore

from factory.agents.errors import AgentError
from factory.agents.registry import TRIAGE
from factory.agents.run import run_agent
from factory.agents.spend import spend_limits
from factory.agents.tools import AgentDeps
from factory.config_model import SpendSettings
from factory.db import Db
from factory.events import EventIds
from tests.test_agents import TRIAGE_OUTPUT, LocalSandbox

EVENT_IDS = EventIds("trace-1", "run-1", "acme/repo", "issue#1")


def listing_then_submit(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if len(_messages) == 1:
        return ModelResponse(parts=[ToolCallPart("list_files", {"path": "."})])
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, TRIAGE_OUTPUT)])


def submit(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, TRIAGE_OUTPUT)])


def test_budgets_follow_the_configuration() -> None:
    assert spend_limits(SpendSettings(), InMemorySpendStore()) is None
    limits = spend_limits(
        SpendSettings(agent_run_usd=Decimal(5), repo_day_usd=Decimal(50), day_usd=Decimal(80)), InMemorySpendStore()
    )
    assert limits is not None
    assert [(budget.name, budget.window, budget.usd, budget.scope is not None) for budget in limits.budgets] == [
        ("agent_run", "run", Decimal(5), False),
        ("repository", "day", Decimal(50), True),
        ("deployment", "day", Decimal(80), False),
    ]


def test_spend_settings_reject_non_positive_ceilings() -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        SpendSettings(day_usd=Decimal(0))


async def test_an_exhausted_budget_stops_the_agent_as_a_spend_limit(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    limits = SpendLimits[AgentDeps](budgets=[Budget(tokens=1, window="run")], store=InMemorySpendStore())
    with pytest.raises(AgentError) as raised:
        await run_agent(
            TRIAGE,
            "triage",
            event_ids=EVENT_IDS,
            sandbox=LocalSandbox(tmp_path / "repo"),
            model=FunctionModel(listing_then_submit),
            spend=limits,
        )
    assert raised.value.code == "spend_limit"


@pytest.mark.postgres
async def test_runs_share_a_daily_budget_counted_in_factory_db(db: Db, tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    store = PostgresSpendStore(cast(Any, await db.pool()), table="spend_test")
    budget = Budget[AgentDeps](tokens=1, window="day", scope=lambda ctx: ctx.deps.repo, name="repository")

    async def triage(repo: str) -> None:
        await run_agent(
            TRIAGE,
            "triage",
            event_ids=EventIds("trace-1", "run-1", repo, "issue#1"),
            sandbox=LocalSandbox(tmp_path / "repo"),
            model=FunctionModel(submit),
            spend=SpendLimits[AgentDeps](budgets=[budget], store=store),
        )

    await triage("acme/repo")
    with pytest.raises(AgentError) as raised:
        await triage("acme/repo")
    assert raised.value.code == "spend_limit"
    await triage("acme/other")
    counters = await db.query("select count(*) as rows from spend_test_counters")
    assert counters[0]["rows"] == 2
