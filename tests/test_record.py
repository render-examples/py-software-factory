from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from factory.agent_runs.record import OnEvent, run_recorded
from factory.agents import AgentError
from factory.db import Db
from factory.runner.protocol import ErrorEvent, ResultEvent, StartedEvent, ToolCallEvent, Usage, UsageEvent
from factory.threads import ThreadKey, thread_key
from tests.fixtures import REPO

pytestmark = pytest.mark.postgres

AT = datetime(2026, 1, 1, tzinfo=UTC)


class Ids:
    run_id = "trn-1"

    @property
    def thread_key(self) -> ThreadKey:
        return thread_key(f"github:{REPO}#issue-7")


async def test_a_finished_agent_run_is_recorded_with_its_turns_usage_and_cost(db: Db) -> None:
    async def call(on_event: OnEvent) -> str:
        on_event(StartedEvent(at=AT, agent_id="triage", harness="pydantic-ai", harness_version="2.68", model="m"))
        on_event(ToolCallEvent(at=AT, tool_call_id="c1", tool="read", args={"path": "README.md"}))
        usage = Usage(input_tokens=10, output_tokens=5, requests=2, cost_usd=0.25)
        on_event(UsageEvent(at=AT, usage=usage))
        on_event(ResultEvent(at=AT, output={"decision": "draft"}, turns=2))
        return "output"

    result = await run_recorded(Ids(), agent_id="triage", model="m", placement="in-task", call=call, db=db)

    assert result == "output"
    [row] = await db.query("select run_id, thread, agent_id, state, turns, cost_usd, tool_calls, usage from agent_runs")
    assert row == {
        "run_id": "trn-1",
        "thread": f"github:{REPO}#issue-7",
        "agent_id": "triage",
        "state": "ok",
        "turns": 2,
        "cost_usd": Decimal("0.25"),
        "tool_calls": 1,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cost_usd": 0.25,
        },
    }


async def test_a_failed_agent_run_is_recorded_with_its_error_code_and_reraised(db: Db) -> None:
    async def call(on_event: OnEvent) -> str:
        on_event(ErrorEvent(at=AT, code="max_turns", message="ran out"))
        raise AgentError("triage", "max_turns", "ran out")

    with pytest.raises(AgentError):
        await run_recorded(Ids(), agent_id="triage", model="m", placement="in-task", call=call, db=db)
    [row] = await db.query("select state from agent_runs")
    assert row["state"] == "max_turns"
