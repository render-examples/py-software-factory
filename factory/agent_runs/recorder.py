"""Records one agent run as an `agent_run:<id>` stream, and sends its token deltas live over `NOTIFY`.

Recording never fails the agent run: failures log a warning. While the database is unreachable, display events are
dropped once the queue is full, but the run's start and its outcome and cost are always kept for the next write.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from factory.db import Db, NewEvent, Tx, append_many
from factory.db.store import encode_payload
from factory.events import (
    ID_MAX,
    LIVE_CHANNEL,
    MESSAGE_MAX,
    TOOL_TEXT_MAX,
    EventSpec,
    LiveDelta,
    LiveFinal,
    agent_run_finished,
    agent_run_started,
    assistant_message,
    tool_call,
    tool_result,
    usage,
    warn,
)
from factory.events.catalog import TokenUsage
from factory.runner.protocol import RunnerEvent
from factory.threads import ThreadKey, is_thread_key

FLUSH_SECONDS = 0.25
MAX_QUEUED = 1000
MAX_PART_BYTES = 6000
MESSAGE_FIELD_MAX = 2000
OUTCOME_MAX = 64
TRUNCATED = "…[truncated]"

Placement = Literal["in-task", "in-sandbox"]


@dataclass(frozen=True)
class AgentRunSummary:
    """How the run ended, as the host measured it; streamed `usage` events are progress only."""

    outcome: str
    turns: int
    usage: TokenUsage
    cost_usd: float
    harness_version: str | None = None
    message: str | None = None
    invalid_lines: int | None = None
    sandbox_requests: int | None = None


class AgentRunRecorder:
    def __init__(self, db: Db, run_id: str, thread: ThreadKey | None = None) -> None:
        self.id = f"ar_{uuid4()}"
        self.stream = f"agent_run:{self.id}"
        self._db = db
        self._run_id = run_id
        self._source = f"workflow:{run_id}"
        self._thread: ThreadKey | None = None
        if thread is None or is_thread_key(thread):
            self._thread = thread
        else:
            warn("agent run recorded without its thread: invalid thread key", agent_run_id=self.id)
        self._queue: list[NewEvent[Any]] = []
        self._deltas: dict[str, str] = {}
        self._open: dict[str, None] = {}
        self._finals: list[str] = []
        self._local_seq = 0
        self._dropped = 0
        self._timer: asyncio.TimerHandle | None = None
        self._flushes: set[asyncio.Task[bool]] = set()
        self._writing = asyncio.Lock()
        self._finished = False

    def start(self, agent_id: str, harness: str, model: str, placement: Placement | None = None) -> None:
        """Queues `agent_run.started`. Never raises."""
        payload = {
            "run_id": clip(self._run_id, ID_MAX),
            "agent_id": clip(agent_id, ID_MAX),
            "harness": clip(harness, ID_MAX),
            "model": clip(model, ID_MAX),
        }
        if placement is not None:
            payload["placement"] = placement
        try:
            self._push(agent_run_started, payload)
        except Exception as error:
            warn("agent run start not recorded", agent_run_id=self.id, error=str(error))

    def on_runner_event(self, event: RunnerEvent) -> None:
        """Queues the log events for one runner event. Never raises."""
        try:
            self._on_runner_event(event)
        except Exception as error:
            warn("agent run event not recorded", agent_run_id=self.id, type=event.type, error=str(error))

    async def finish(self, summary: AgentRunSummary) -> None:
        """Records the outcome and writes everything still queued. Never raises."""
        if self._finished:
            return
        try:
            await self._finish(summary)
        except Exception as error:
            warn("agent run outcome not recorded", agent_run_id=self.id, error=str(error))

    def _on_runner_event(self, event: RunnerEvent) -> None:
        if event.type == "assistant_delta":
            message_id = clip(event.message_id, ID_MAX)
            self._deltas[message_id] = self._deltas.get(message_id, "") + event.text
            self._open[message_id] = None
            self._schedule()
        elif event.type == "assistant_text":
            message_id = clip(event.message_id, ID_MAX)
            self._push(assistant_message, {"message_id": message_id, "text": clip(event.text, MESSAGE_MAX)})
            if message_id in self._open:
                del self._open[message_id]
                self._finals.append(message_id)
        elif event.type == "tool_call":
            args = json.dumps(event.args, ensure_ascii=False, separators=(",", ":"), default=str)
            self._push(
                tool_call,
                {
                    "call_id": clip(event.tool_call_id, ID_MAX),
                    "tool": clip(event.tool, ID_MAX),
                    "args": clip(args, TOOL_TEXT_MAX),
                },
            )
        elif event.type == "tool_result":
            self._push(
                tool_result,
                {
                    "call_id": clip(event.tool_call_id, ID_MAX),
                    "tool": clip(event.tool, ID_MAX),
                    "is_error": event.is_error,
                    "output_preview": clip(event.output_preview, TOOL_TEXT_MAX),
                },
            )
        elif event.type == "usage":
            spent = event.usage
            self._push(
                usage,
                {
                    "input_tokens": spent.input_tokens,
                    "output_tokens": spent.output_tokens,
                    "cache_read_tokens": spent.cache_read_tokens,
                    "cache_write_tokens": spent.cache_write_tokens,
                    "cost_usd": spent.cost_usd,
                },
            )

    async def _finish(self, summary: AgentRunSummary) -> None:
        self._finished = True
        self._finals.extend(self._open)
        self._open.clear()
        payload: dict[str, Any] = {
            "outcome": clip(summary.outcome, OUTCOME_MAX),
            "turns": summary.turns,
            "usage": summary.usage.model_dump(mode="json"),
            "cost_usd": summary.cost_usd,
        }
        if summary.harness_version:
            payload["harness_version"] = clip(summary.harness_version, ID_MAX)
        if summary.message:
            payload["message"] = clip(summary.message, MESSAGE_FIELD_MAX)
        if summary.invalid_lines:
            payload["invalid_lines"] = summary.invalid_lines
        if summary.sandbox_requests is not None:
            payload["sandbox_requests"] = summary.sandbox_requests
        self._push(agent_run_finished, payload)
        if not await self._flush():
            self._queue = [event for event in self._queue if _lifecycle(event)]
            await self._flush()
        if self._flushes:
            await asyncio.gather(*self._flushes, return_exceptions=True)

    def _push(self, spec: EventSpec[Any], payload: dict[str, Any]) -> None:
        event = NewEvent(spec, self.stream, self._source, payload, self._thread)
        try:
            event = NewEvent(spec, self.stream, self._source, encode_payload(event), self._thread)
        except ValueError as error:
            warn("agent run event dropped: invalid payload", agent_run_id=self.id, type=spec.type, error=str(error))
            return
        if len(self._queue) >= MAX_QUEUED and not _lifecycle(event):
            self._dropped += 1
            return
        self._queue.append(event)
        self._schedule()

    def _schedule(self) -> None:
        if self._finished or self._timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timer = loop.call_later(FLUSH_SECONDS, self._flush_later, loop)

    def _flush_later(self, loop: asyncio.AbstractEventLoop) -> None:
        self._timer = None
        task = loop.create_task(self._flush())
        self._flushes.add(task)
        task.add_done_callback(self._flushes.discard)

    async def _flush(self) -> bool:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        async with self._writing:
            return await self._write()

    async def _write(self) -> bool:
        events, deltas, finals = self._queue, self._deltas, self._finals
        if not events and not deltas and not finals:
            return True
        self._queue, self._deltas, self._finals = [], {}, []
        try:
            async with self._db.transaction() as tx:
                for message_id, text in deltas.items():
                    self._local_seq += 1
                    for part, chunk in enumerate(_chunks(clean(text))):
                        notice = LiveDelta(
                            stream=self.stream,
                            thread=self._thread,
                            message_id=message_id,
                            local_seq=self._local_seq,
                            part=part,
                            text=chunk,
                        )
                        await _notify(tx, notice)
                await append_many(tx, self.stream, events)
                for message_id in finals:
                    await _notify(tx, LiveFinal(stream=self.stream, message_id=message_id))
        except Exception as error:
            requeued = [*events, *self._queue]
            self._queue = [event for index, event in enumerate(requeued) if index < MAX_QUEUED or _lifecycle(event)]
            self._dropped += len(requeued) - len(self._queue)
            self._finals = [*finals, *self._finals]
            warn("agent run events not recorded", agent_run_id=self.id, queued=len(self._queue), error=str(error))
            self._schedule()
            return False
        if self._dropped:
            warn(
                "agent run events dropped while the database was unreachable",
                agent_run_id=self.id,
                dropped=self._dropped,
            )
            self._dropped = 0
        return True


def _lifecycle(event: NewEvent[Any]) -> bool:
    return event.type in (agent_run_started, agent_run_finished)


async def _notify(tx: Tx, notice: LiveDelta | LiveFinal) -> None:
    await tx.query("select pg_notify(%s, %s)", (LIVE_CHANNEL, notice.model_dump_json()))


def _chunks(text: str) -> list[str]:
    """Splits text into parts of at most MAX_PART_BYTES JSON-encoded bytes, under the 8,000-byte NOTIFY limit."""
    parts: list[str] = []
    start = 0
    size = 0
    for index, char in enumerate(text):
        width = _json_char_bytes(char)
        if size + width > MAX_PART_BYTES:
            parts.append(text[start:index])
            start, size = index, 0
        size += width
    if start < len(text):
        parts.append(text[start:])
    return parts


_SURROGATE = re.compile("[\ud800-\udfff]")


def clean(text: str) -> str:
    """Drops NUL characters and replaces unpaired surrogates, both of which Postgres rejects."""
    text = text.replace("\x00", "")
    if _SURROGATE.search(text) is None:
        return text
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def clip(text: str, max_bytes: int) -> str:
    """Cleans text and cuts it, at a whole character, to at most `max_bytes` bytes once JSON-encoded as UTF-8."""
    safe = clean(text)
    if json_bytes(safe) <= max_bytes:
        return safe
    budget = max_bytes - json_bytes(TRUNCATED)
    end = 0
    size = 0
    for char in safe:
        size += _json_char_bytes(char)
        if size > budget:
            break
        end += 1
    return safe[:end] + TRUNCATED


def json_bytes(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode()) - 2


_SHORT_ESCAPES = frozenset('"\\\b\f\n\r\t')


def _json_char_bytes(char: str) -> int:
    if char in _SHORT_ESCAPES:
        return 2
    if char < " ":
        return 6
    return len(char.encode())
