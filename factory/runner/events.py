"""Maps Pydantic AI stream events to runner protocol events, for both agent placements.

Shipped in the runner bundle: import only the standard library, pydantic_ai, and bundled factory packages.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterable, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai import __version__ as pydantic_ai_version
from pydantic_ai.messages import (
    AgentStreamEvent,
    FinalResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelResponsePart,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.usage import RunUsage

from factory.runner.protocol import (
    AssistantDeltaEvent,
    AssistantTextEvent,
    RunnerEvent,
    StartedEvent,
    ToolCallEvent,
    ToolResultEvent,
    Usage,
)

Emit = Callable[[RunnerEvent], Awaitable[None] | None]
StreamHandler = Callable[[RunContext[Any], AsyncIterable[AgentStreamEvent]], Awaitable[None]]

PREVIEW_LIMIT = 2_000


class RunnerEventMapper:
    """Maps one agent run's stream events to `RunnerEvent`s.

    Each text part streams as `assistant_delta`s and then gets exactly one `assistant_text` with its full
    text, both under the message ID `m<response>-<part>`. Responses are numbered from 1 in arrival order.
    """

    def __init__(self) -> None:
        self._response = 0
        self._in_response = False
        self._open: dict[int, list[str]] = {}

    def map(self, event: AgentStreamEvent) -> list[RunnerEvent]:
        if isinstance(event, FunctionToolCallEvent):
            return [
                *self.end_response(),
                ToolCallEvent(
                    at=now(),
                    tool_call_id=event.part.tool_call_id,
                    tool=event.part.tool_name,
                    args=event.part.args_as_dict(),
                ),
            ]
        if isinstance(event, FunctionToolResultEvent):
            return [
                *self.end_response(),
                ToolResultEvent(
                    at=now(),
                    tool_call_id=event.part.tool_call_id,
                    tool=event.part.tool_name,
                    is_error=event.part.outcome != "success",
                    output_preview=preview(event.part.model_response_str()),
                ),
            ]
        if isinstance(event, PartStartEvent):
            return self._start_part(event.index, event.part)
        if isinstance(event, PartDeltaEvent):
            self._enter_response()
            if not isinstance(event.delta, TextPartDelta) or not event.delta.content_delta:
                return []
            return [self._append(event.index, event.delta.content_delta)]
        if isinstance(event, PartEndEvent):
            return self._end_part(event.index, event.part)
        if isinstance(event, FinalResultEvent):
            self._enter_response()
        return []

    def end_response(self) -> list[RunnerEvent]:
        """Finish every open text part. Call when a model response, or the whole run, ends."""
        self._in_response = False
        return self._finish_open()

    def stream_handler(self, emit: Emit) -> StreamHandler:
        """An `event_stream_handler` for `Agent.run`. Pydantic AI calls it once per graph node, so each
        model response ends when its call returns."""

        async def handle(_ctx: RunContext[Any], events: AsyncIterable[AgentStreamEvent]) -> None:
            async for event in events:
                for mapped in self.map(event):
                    await _deliver(emit, mapped)
            for mapped in self.end_response():
                await _deliver(emit, mapped)

        return handle

    def _enter_response(self) -> None:
        if not self._in_response:
            self._response += 1
            self._in_response = True

    def _start_part(self, index: int, part: ModelResponsePart) -> list[RunnerEvent]:
        self._enter_response()
        self._open.pop(index, None)
        events = self._finish_open()
        if isinstance(part, TextPart):
            self._open[index] = []
            if part.content:
                events.append(self._append(index, part.content))
        return events

    def _append(self, index: int, text: str) -> AssistantDeltaEvent:
        self._open.setdefault(index, []).append(text)
        return AssistantDeltaEvent(at=now(), message_id=self._message_id(index), text=text)

    def _end_part(self, index: int, part: ModelResponsePart) -> list[RunnerEvent]:
        self._enter_response()
        if self._open.pop(index, None) is None or not isinstance(part, TextPart) or not part.content:
            return []
        return [AssistantTextEvent(at=now(), message_id=self._message_id(index), text=part.content)]

    def _finish_open(self) -> list[RunnerEvent]:
        finals: list[RunnerEvent] = []
        for index, chunks in sorted(self._open.items()):
            if text := "".join(chunks):
                finals.append(AssistantTextEvent(at=now(), message_id=self._message_id(index), text=text))
        self._open.clear()
        return finals

    def _message_id(self, index: int) -> str:
        return f"m{self._response}-{index}"


async def _deliver(emit: Emit, event: RunnerEvent) -> None:
    delivered = emit(event)
    if inspect.isawaitable(delivered):
        await delivered


def started_event(agent_id: str, model: str) -> StartedEvent:
    return StartedEvent(
        at=now(),
        agent_id=agent_id,
        harness="pydantic-ai",
        harness_version=pydantic_ai_version,
        model=model,
    )


def usage_from(usage: RunUsage) -> Usage:
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        requests=usage.requests,
        tool_calls=usage.tool_calls,
        cost_usd=float(usage.cost) if usage.cost is not None else None,
    )


def preview(text: str) -> str:
    suffix = "…[truncated]"
    return text if len(text) <= PREVIEW_LIMIT else text[: PREVIEW_LIMIT - len(suffix)] + suffix


def now() -> datetime:
    return datetime.now(UTC)
