from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from psycopg.types.json import Jsonb

import factory.agent_runs.recorder as recorder_module
from factory.agent_runs.recorder import MAX_PART_BYTES, AgentRunRecorder, AgentRunSummary, json_bytes
from factory.db import Db, LogEvent, Params, Query, Row, Tx, read_stream
from factory.db.read import to_event
from factory.events import LIVE_CHANNEL, LIVE_NOTICE_ADAPTER, LiveDelta, LiveFinal
from factory.events.catalog import TokenUsage
from factory.runner.protocol import (
    AssistantDeltaEvent,
    AssistantTextEvent,
    ErrorEvent,
    ResultEvent,
    RunnerEvent,
    StartedEvent,
    ToolCallEvent,
    ToolResultEvent,
    Usage,
    UsageEvent,
)
from factory.threads import ThreadKey

AT = datetime.now(UTC)
THREAD = ThreadKey("github:acme/web#issue-12")


def token_usage(cost: float | None) -> TokenUsage:
    return TokenUsage(input_tokens=100, output_tokens=10, cost_usd=cost)


SUMMARY = AgentRunSummary(outcome="ok", turns=1, usage=token_usage(0.01), cost_usd=0.01)
USAGE_PAYLOAD = {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 0, "cache_write_tokens": 0}


Committed = LogEvent | LiveDelta | LiveFinal


class FakeTx:
    def __init__(self, versions: dict[str, int], seq: int) -> None:
        self.versions = dict(versions)
        self.seq = seq
        self.log: list[Committed] = []

    async def query(self, query: Query, params: Params = None) -> list[Row]:
        assert isinstance(query, str)
        if query.startswith("insert into events"):
            assert isinstance(params, dict)
            stream = str(params["stream"])
            self.versions[stream] = self.versions.get(stream, 0) + 1
            self.seq += 1
            payload = params["payload"]
            assert isinstance(payload, Jsonb)
            row: Row = {
                "seq": self.seq,
                "tx": "1",
                "stream": stream,
                "stream_version": self.versions[stream],
                "thread": params["thread"],
                "type": params["type"],
                "version": params["version"],
                "source": params["source"],
                "payload": payload.obj,
                "created_at": AT,
            }
            self.log.append(to_event(row))
            return [row]
        if "pg_notify" in query:
            assert isinstance(params, tuple)
            channel, payload = params
            if channel == LIVE_CHANNEL:
                self.log.append(LIVE_NOTICE_ADAPTER.validate_json(str(payload)))
        return []


class FakeDb:
    """Captures what each committed transaction wrote. The first `failures` transactions fail to connect."""

    def __init__(self, failures: int = 0, error: Exception | None = None) -> None:
        self.failures = failures
        self.error = error or ConnectionError("connection refused")
        self.attempts = 0
        self.log: list[Committed] = []
        self._versions: dict[str, int] = {}
        self._seq = 0

    @property
    def events(self) -> list[LogEvent]:
        return [entry for entry in self.log if isinstance(entry, LogEvent)]

    @property
    def deltas(self) -> list[LiveDelta]:
        return [entry for entry in self.log if isinstance(entry, LiveDelta)]

    async def query(self, query: Query, params: Params = None) -> list[Row]:
        raise AssertionError("the recorder only writes in transactions")

    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        return self._transaction()

    @asynccontextmanager
    async def _transaction(self) -> AsyncGenerator[Tx]:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.error
        tx = FakeTx(self._versions, self._seq)
        yield tx
        self._versions, self._seq = tx.versions, tx.seq
        self.log.extend(tx.log)

    async def aclose(self) -> None:
        pass


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(recorder_module, "warn", lambda msg, **fields: seen.append((msg, fields)))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    return seen


def recorder(db: Db, thread: ThreadKey | None = THREAD) -> AgentRunRecorder:
    r = AgentRunRecorder(db, "trn-1", thread)
    r.start("triage", "pydantic-ai", "anthropic:claude-opus-5")
    return r


def feed(r: AgentRunRecorder, *events: RunnerEvent) -> None:
    for event in events:
        r.on_runner_event(event)


def delta(text: str, message_id: str = "m1") -> AssistantDeltaEvent:
    return AssistantDeltaEvent(at=AT, message_id=message_id, text=text)


def text(value: str, message_id: str = "m1") -> AssistantTextEvent:
    return AssistantTextEvent(at=AT, message_id=message_id, text=value)


def call(call_id: str, args: Any = None) -> ToolCallEvent:
    return ToolCallEvent(at=AT, tool_call_id=call_id, tool="ls", args={} if args is None else args)


def payload_of(events: list[LogEvent], type: str) -> dict[str, Any]:
    return next(event.payload for event in events if event.type == type)


async def until(check: Callable[[], bool], within: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError
        await asyncio.sleep(0.01)


async def test_runner_events_become_log_events_on_the_run_stream(warnings: list[Any]) -> None:
    db = FakeDb()
    r = recorder(db)
    feed(
        r,
        StartedEvent(at=AT, agent_id="evil", harness="pi", harness_version="1", model="x"),
        call("c1", {"path": "."}),
        ToolResultEvent(at=AT, tool_call_id="c1", tool="ls", is_error=False, output_preview="README.md"),
        text("Done."),
        UsageEvent(at=AT, usage=Usage(input_tokens=100, output_tokens=10, requests=1, tool_calls=1, cost_usd=0.01)),
        ResultEvent(at=AT, output={}, turns=1),
        ErrorEvent(at=AT, code="agent_error", message="ignored"),
    )
    await r.finish(SUMMARY)
    assert [(e.type, e.payload) for e in db.events] == [
        (
            "agent_run.started",
            {"run_id": "trn-1", "agent_id": "triage", "harness": "pydantic-ai", "model": "anthropic:claude-opus-5"},
        ),
        ("tool.call", {"call_id": "c1", "tool": "ls", "args": '{"path":"."}'}),
        ("tool.result", {"call_id": "c1", "tool": "ls", "is_error": False, "output_preview": "README.md"}),
        ("assistant.message", {"message_id": "m1", "text": "Done."}),
        ("usage", {**USAGE_PAYLOAD, "cost_usd": 0.01}),
        (
            "agent_run.finished",
            {"outcome": "ok", "turns": 1, "usage": {**USAGE_PAYLOAD, "cost_usd": 0.01}, "cost_usd": 0.01},
        ),
    ]
    assert {(e.stream, e.thread, e.source) for e in db.events} == {(r.stream, THREAD, "workflow:trn-1")}
    assert re.fullmatch(r"ar_[0-9a-f-]{36}", r.id)
    assert warnings == []


async def test_a_long_delta_is_split_into_parts_that_each_fit_a_notify_payload() -> None:
    long = '"' * 9000 + "é" * 5000 + "🙂"
    db = FakeDb()
    r = recorder(db)
    feed(r, delta(long))
    await r.finish(SUMMARY)
    parts = db.deltas
    assert len(parts) > 2
    assert [p.part for p in parts] == list(range(len(parts)))
    assert {p.local_seq for p in parts} == {1}
    assert "".join(p.text for p in parts) == long
    assert max(json_bytes(p.text) for p in parts) <= MAX_PART_BYTES
    assert max(len(p.model_dump_json().encode()) for p in parts) < 8000


async def test_each_flush_sends_a_message_s_new_deltas_under_a_higher_local_seq() -> None:
    db = FakeDb()
    r = recorder(db)
    feed(r, delta("Hel"), delta("lo"))
    await until(lambda: len(db.deltas) == 1)
    feed(r, delta(" there"))
    await r.finish(SUMMARY)
    assert [(d.local_seq, d.part, d.text) for d in db.deltas] == [(1, 0, "Hello"), (2, 0, " there")]


async def test_deltas_go_before_the_events_and_finals_after_them_in_one_transaction() -> None:
    db = FakeDb()
    r = recorder(db)
    feed(r, delta("Hi"), text("Hi"), delta("Wor", "m2"))
    await r.finish(SUMMARY)
    kinds = [e.type if isinstance(e, LogEvent) else f"{e.type}:{e.message_id}" for e in db.log]
    assert kinds == [
        "delta:m1",
        "delta:m2",
        "agent_run.started",
        "assistant.message",
        "agent_run.finished",
        "final:m1",
        "final:m2",
    ]
    assert db.attempts == 1


def _cleaned(db: FakeDb) -> bool:
    message = payload_of(db.events, "assistant.message")
    return message == {"message_id": "m1", "text": "ab \ufffd"} and db.deltas[0].text == "ab"


def _tool_output_clipped(db: FakeDb) -> bool:
    preview = payload_of(db.events, "tool.result")["output_preview"]
    return 32 * 1024 - 8 < json_bytes(preview) <= 32 * 1024 and re.fullmatch(r"x+…\[truncated\]", preview) is not None


def _escapes_clipped(db: FakeDb) -> bool:
    types = [e.type for e in db.events]
    return (
        types == ["agent_run.started", "assistant.message", "tool.call", "tool.result", "agent_run.finished"]
        and json_bytes(payload_of(db.events, "assistant.message")["text"]) <= 64 * 1024
        and json_bytes(payload_of(db.events, "tool.call")["args"]) <= 32 * 1024
    )


def _cut_at_a_character(db: FakeDb) -> bool:
    message = payload_of(db.events, "assistant.message")["text"]
    return json_bytes(message) <= 64 * 1024 and re.fullmatch(r"字+🙂+…\[truncated\]", message) is not None


@pytest.mark.parametrize(
    ("events", "check"),
    [
        pytest.param(
            [delta("a\x00b"), text("a\x00b \ud83d")],
            _cleaned,
            id="strips NUL characters and unpaired surrogates, which Postgres rejects",
        ),
        pytest.param(
            [
                ToolResultEvent(
                    at=AT, tool_call_id="c1", tool="bash", is_error=True, output_preview="\x00" + "x" * 40_000
                )
            ],
            _tool_output_clipped,
            id="truncates long tool output to its byte budget",
        ),
        pytest.param(
            [
                text("\x1b" * 60_000),
                call("c1", {"command": "\x1b" * 40_000}),
                ToolResultEvent(at=AT, tool_call_id="c1", tool="bash", is_error=False, output_preview="ok"),
            ],
            _escapes_clipped,
            id="clips by JSON-encoded bytes, so ANSI escapes can't block the events after them",
        ),
        pytest.param(
            [text("字" * 20_000 + "🙂" * 20_000)],
            _cut_at_a_character,
            id="cuts multi-byte text at a character boundary within the budget",
        ),
    ],
)
async def test_text_is_cleaned_and_clipped(
    events: list[RunnerEvent], check: Callable[[FakeDb], bool], warnings: list[Any]
) -> None:
    db = FakeDb()
    r = recorder(db)
    feed(r, *events)
    await r.finish(SUMMARY)
    assert check(db)
    assert warnings == []


async def test_a_run_with_a_malformed_thread_key_is_recorded_without_its_thread(warnings: list[Any]) -> None:
    db = FakeDb()
    r = recorder(db, ThreadKey("not a key"))
    feed(r, text("Done."))
    await r.finish(SUMMARY)
    assert [(e.type, e.thread) for e in db.events] == [
        ("agent_run.started", None),
        ("assistant.message", None),
        ("agent_run.finished", None),
    ]
    assert [msg for msg, _ in warnings] == ["agent run recorded without its thread: invalid thread key"]


async def test_agent_run_finished_comes_from_the_host_s_summary_not_streamed_usage() -> None:
    db = FakeDb()
    r = recorder(db)
    feed(r, UsageEvent(at=AT, usage=Usage(input_tokens=5, output_tokens=5, cost_usd=5)))
    summary = AgentRunSummary(
        outcome="max_turns",
        turns=1,
        usage=token_usage(None),
        cost_usd=0.01,
        harness_version="pydantic-ai 2.68.0",
        message="turn limit (1) reached",
        invalid_lines=0,
        sandbox_requests=7,
    )
    await r.finish(summary)
    assert db.events[-1].payload == {
        "outcome": "max_turns",
        "turns": 1,
        "usage": USAGE_PAYLOAD,
        "cost_usd": 0.01,
        "harness_version": "pydantic-ai 2.68.0",
        "message": "turn limit (1) reached",
        "sandbox_requests": 7,
    }


async def test_a_failed_write_is_retried_with_its_finals(warnings: list[tuple[str, dict[str, Any]]]) -> None:
    db = FakeDb(failures=1)
    r = recorder(db)
    feed(r, delta("Hi"), text("Hi"), call("c1"))
    await until(lambda: db.attempts >= 1)
    feed(r, call("c2"))
    await r.finish(SUMMARY)
    kinds = [e.type for e in db.log]
    assert kinds == ["agent_run.started", "assistant.message", "tool.call", "tool.call", "agent_run.finished", "final"]
    assert [msg for msg, _ in warnings] == ["agent run events not recorded"]


async def test_the_outcome_is_recorded_even_when_the_rest_of_the_stream_can_t_be() -> None:
    db = FakeDb(failures=2)
    r = recorder(db)
    for i in range(1005):
        feed(r, call(f"c{i}"))
    await until(lambda: db.attempts >= 1)
    await r.finish(SUMMARY)
    assert [e.type for e in db.events] == ["agent_run.started", "agent_run.finished"]


async def test_the_queue_is_bounded_while_the_database_is_down_but_never_drops_the_lifecycle(
    warnings: list[tuple[str, dict[str, Any]]],
) -> None:
    db = FakeDb(failures=1)
    r = recorder(db)
    for i in range(1005):
        feed(r, call(f"c{i}"))
    await until(lambda: db.attempts >= 1)
    await r.finish(SUMMARY)
    types = [e.type for e in db.events]
    assert len(types) == 1001
    assert (types[0], types[-1]) == ("agent_run.started", "agent_run.finished")
    assert ("agent run events dropped while the database was unreachable", {"agent_run_id": r.id, "dropped": 6}) in (
        warnings
    )


async def test_recording_never_raises_and_leaves_no_task_behind(warnings: list[tuple[str, dict[str, Any]]]) -> None:
    db = FakeDb(failures=10, error=RuntimeError("boom"))
    r = recorder(db)
    feed(r, delta("Hi"), call("c1", {"value": object()}), text("Hi"))
    await until(lambda: db.attempts >= 1)
    await r.finish(SUMMARY)
    await r.finish(SUMMARY)
    assert db.attempts == 3
    assert [msg for msg, _ in warnings] == ["agent run events not recorded"] * 3
    assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []


def test_runner_events_outside_an_event_loop_are_queued_without_raising() -> None:
    r = recorder(FakeDb())
    feed(r, delta("Hi"), text("Hi"), call("c1"))
    assert [event.type.type for event in r._queue] == ["agent_run.started", "assistant.message", "tool.call"]


@pytest.mark.postgres
async def test_a_recorded_run_produces_its_stream_and_agent_runs_row(db: Db) -> None:
    r = recorder(db)
    feed(
        r,
        delta("Look"),
        text("Looking."),
        call("c1", {"path": "."}),
        ToolResultEvent(at=AT, tool_call_id="c1", tool="ls", is_error=False, output_preview="README.md"),
        call("c2", {"path": "src"}),
        UsageEvent(at=AT, usage=Usage(input_tokens=100, output_tokens=10, cost_usd=0.02)),
    )
    await r.finish(AgentRunSummary(outcome="ok", turns=2, usage=token_usage(0.02), cost_usd=0.02, sandbox_requests=3))
    events = await read_stream(db, r.stream, 0)
    assert [e.type for e in events] == [
        "agent_run.started",
        "assistant.message",
        "tool.call",
        "tool.result",
        "tool.call",
        "usage",
        "agent_run.finished",
    ]
    assert [e.stream_version for e in events] == list(range(1, 8))
    assert {e.thread for e in events} == {THREAD}
    rows = await db.query(
        "select run_id, thread, agent_id, model, state, turns, usage, cost_usd, tool_calls, sandbox_requests, "
        "finished_at is not null as finished from agent_runs where id = %s",
        (r.id,),
    )
    assert rows == [
        {
            "run_id": "trn-1",
            "thread": THREAD,
            "agent_id": "triage",
            "model": "anthropic:claude-opus-5",
            "state": "ok",
            "turns": 2,
            "usage": {**USAGE_PAYLOAD, "cost_usd": 0.02},
            "cost_usd": Decimal("0.02"),
            "tool_calls": 2,
            "sandbox_requests": 3,
            "finished": True,
        }
    ]
    assert events[2].payload == {"call_id": "c1", "tool": "ls", "args": '{"path":"."}'}
