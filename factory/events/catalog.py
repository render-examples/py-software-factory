"""Every event the log records. A payload's shape is part of the log's contract: change it with a new version."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from factory.threads import is_thread_key


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


P = TypeVar("P", bound=Payload)


@dataclass(frozen=True)
class EventSpec(Generic[P]):
    type: str
    version: int
    payload: type[P]

    def parse(self, data: object) -> P:
        return self.payload.model_validate(data)


def define_event(type: str, version: int, payload: type[P]) -> EventSpec[P]:
    return EventSpec(type, version, payload)


ID_MAX = 200
MESSAGE_MAX = 64 * 1024
TOOL_TEXT_MAX = 32 * 1024

Id = Annotated[str, Field(min_length=1, max_length=ID_MAX)]
Name = Annotated[str, Field(min_length=1, max_length=100)]
Summary = Annotated[str, Field(max_length=16_000)]
Count = Annotated[int, Field(ge=0)]
FinalState = Literal["done", "failed", "cancelled"]
RunMode = Literal["replace", "continue", "reply"]


class DeliveryReceived(Payload):
    event: Name
    repo: Id | None = None
    task: Name | None = None
    input: dict[str, Any] | None = None
    idempotency_key: Annotated[str, Field(max_length=400)] | None = None
    ignored: Annotated[str, Field(max_length=200)] | None = None


class RunStarted(Payload):
    run_id: Id
    trace_id: Id
    task: Name
    title: Name
    actor: Name
    mode: RunMode


class RunRejected(Payload):
    trace_id: Id
    task: Name
    title: Name
    actor: Name
    reason: Summary


class RunProgress(Payload):
    summary: Summary


class RunAwaitingReply(Payload):
    pass


class RunSandboxAttached(Payload):
    sandbox_id: Id


class Unconfirmed(Payload):
    task_run: Id | None = None
    sandboxes: Annotated[tuple[Id, ...], Field(max_length=50)] = ()


class RunStopped(Payload):
    by: Id
    active: bool
    summary: Summary
    unconfirmed: Unconfirmed | None = None


class RunCleanedUp(Payload):
    by: Id


class Link(Payload):
    label: Annotated[str, Field(max_length=200)]
    url: Annotated[str, Field(max_length=2000)]


class RunFinished(Payload):
    state: FinalState
    outcome: Name
    summary: Summary
    links: Annotated[tuple[Link, ...], Field(max_length=20)] = ()
    duration_ms: Count
    waiting: Literal["reply"] | None = None


class AgentRunStarted(Payload):
    run_id: Id
    agent_id: Id
    harness: Id
    model: Id
    placement: Literal["in-task", "in-sandbox"] | None = None


class AssistantMessage(Payload):
    message_id: Id
    text: Annotated[str, Field(max_length=MESSAGE_MAX)]


class ToolCall(Payload):
    call_id: Id
    tool: Id
    args: Annotated[str, Field(max_length=TOOL_TEXT_MAX)]


class ToolResult(Payload):
    call_id: Id
    tool: Id
    is_error: bool
    output_preview: Annotated[str, Field(max_length=TOOL_TEXT_MAX)]


class TokenUsage(Payload):
    input_tokens: Count
    output_tokens: Count
    cache_read_tokens: Count = 0
    cache_write_tokens: Count = 0
    cost_usd: Annotated[float, Field(ge=0)] | None = None


class AgentRunFinished(Payload):
    outcome: Annotated[str, Field(min_length=1, max_length=64)]
    turns: Count
    usage: TokenUsage
    cost_usd: Annotated[float, Field(ge=0)]
    harness_version: Id | None = None
    message: Annotated[str, Field(max_length=2000)] | None = None
    invalid_lines: Count | None = None
    sandbox_requests: Count | None = None


delivery_received = define_event("delivery.received", 1, DeliveryReceived)

run_started = define_event("run.started", 1, RunStarted)
run_rejected = define_event("run.rejected", 1, RunRejected)
run_progress = define_event("run.progress", 1, RunProgress)
run_awaiting_reply = define_event("run.awaiting_reply", 1, RunAwaitingReply)
run_sandbox_attached = define_event("run.sandbox_attached", 1, RunSandboxAttached)
run_stopped = define_event("run.stopped", 1, RunStopped)
run_cleaned_up = define_event("run.cleaned_up", 1, RunCleanedUp)
run_finished = define_event("run.finished", 1, RunFinished)

agent_run_started = define_event("agent_run.started", 1, AgentRunStarted)
assistant_message = define_event("assistant.message", 1, AssistantMessage)
tool_call = define_event("tool.call", 1, ToolCall)
tool_result = define_event("tool.result", 1, ToolResult)
usage = define_event("usage", 1, TokenUsage)
agent_run_finished = define_event("agent_run.finished", 1, AgentRunFinished)

LIVE_CHANNEL = "live"
"""NOTIFY channel for token deltas. Deltas are never stored."""


class LiveDelta(Payload):
    type: Literal["delta"] = "delta"
    stream: Id
    thread: str | None = None
    message_id: Id
    local_seq: Count
    part: Count
    text: Annotated[str, Field(max_length=8000)]

    @field_validator("thread")
    @classmethod
    def _thread_key(cls, value: str | None) -> str | None:
        if value is not None and not is_thread_key(value):
            raise ValueError("expected a <channel>:<id> thread key")
        return value


class LiveFinal(Payload):
    type: Literal["final"] = "final"
    stream: Id
    message_id: Id


LiveNotice = Annotated[LiveDelta | LiveFinal, Field(discriminator="type")]
LIVE_NOTICE_ADAPTER: TypeAdapter[LiveDelta | LiveFinal] = TypeAdapter(LiveNotice)
