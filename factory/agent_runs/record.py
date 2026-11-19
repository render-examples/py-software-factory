"""Recording one agent run in the event log from the events it streams."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal, Protocol, TypeVar

from factory.agent_runs.recorder import AgentRunRecorder, AgentRunSummary
from factory.db import Db, workflows_db
from factory.events.catalog import TokenUsage
from factory.runner.protocol import ErrorEvent, ResultEvent, RunnerEvent, StartedEvent, Usage, UsageEvent
from factory.threads import ThreadKey

T = TypeVar("T")

OnEvent = Callable[[RunnerEvent], None]
HARNESS = "pydantic-ai"


class RunIds(Protocol):
    @property
    def run_id(self) -> str: ...
    @property
    def thread_key(self) -> ThreadKey: ...


class _Observed:
    """Records every event and keeps what the summary needs. The terminal event matches what the call returned or
    raised, so the summary describes the run the caller saw."""

    def __init__(self, recorder: AgentRunRecorder) -> None:
        self._recorder = recorder
        self.usage = Usage(input_tokens=0, output_tokens=0)
        self.turns = 0
        self.harness_version: str | None = None
        self.error: ErrorEvent | None = None

    def __call__(self, event: RunnerEvent) -> None:
        self._recorder.on_runner_event(event)
        if isinstance(event, StartedEvent):
            self.harness_version = event.harness_version
        elif isinstance(event, UsageEvent):
            self.usage = event.usage
        elif isinstance(event, ResultEvent):
            self.turns = event.turns
        elif isinstance(event, ErrorEvent):
            self.error = event

    def summary(self, outcome: str, message: str | None = None) -> AgentRunSummary:
        usage = self.usage
        cost = usage.cost_usd or 0.0
        return AgentRunSummary(
            outcome=outcome,
            turns=self.turns or usage.requests,
            usage=TokenUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cost_usd=usage.cost_usd,
            ),
            cost_usd=cost,
            harness_version=self.harness_version,
            message=message,
        )


async def run_recorded(
    run: RunIds,
    *,
    agent_id: str,
    model: str,
    placement: Literal["in-task", "in-sandbox"],
    call: Callable[[OnEvent], Awaitable[T]],
    db: Db | None = None,
) -> T:
    """Runs `call(on_event)`, recording the agent run on its own stream under `run`'s thread. Recording failures are
    logged by the recorder and never change the result."""
    recorder = AgentRunRecorder(db or workflows_db(), run.run_id, run.thread_key)
    recorder.start(agent_id, HARNESS, model, placement)
    observed = _Observed(recorder)
    try:
        result = await call(observed)
    except Exception as error:
        outcome = observed.error.code if observed.error else "agent_error"
        await recorder.finish(observed.summary(outcome, str(error)[:2000]))
        raise
    await recorder.finish(observed.summary("ok"))
    return result
