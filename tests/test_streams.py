from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from factory.agent_runs.recorder import AgentRunRecorder, AgentRunSummary
from factory.db import Db, NewEvent, append
from factory.events import Payload, define_event
from factory.events.catalog import TokenUsage
from factory.gateway.hub import EventHub, listen
from factory.gateway.live import PartialMessage
from factory.gateway.stream import StreamDeps, stream_router
from factory.runner.protocol import AssistantDeltaEvent, AssistantTextEvent, RunnerEvent, ToolCallEvent
from factory.threads import ThreadKey

pytestmark = pytest.mark.postgres

TOKEN = "reader-token"
AUTH = {"authorization": f"Bearer {TOKEN}"}
THREAD = ThreadKey("github:acme/web#issue-12")
THREAD_PATH = "/threads/github/acme/web/issue/12/stream"
AGENT_RUN_PATH = "/agent-runs/ar_00000000-0000-0000-0000-000000000000/stream"
AT = datetime.now(UTC)
SUMMARY = AgentRunSummary(outcome="ok", turns=1, usage=TokenUsage(input_tokens=1, output_tokens=1), cost_usd=0)


class Opened(Payload):
    run_id: str


class Progressed(Payload):
    summary: str


opened = define_event("test.opened", 1, Opened)
progressed = define_event("test.progressed", 1, Progressed)


@dataclass(frozen=True)
class Frame:
    event: str | None
    id: str | None
    data: Any


class SseReader:
    """Reads frames in the background, so a test can wait for the next one with a timeout."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self._frames: asyncio.Queue[Frame | None] = asyncio.Queue()
        self._task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        fields: dict[str, str] = {}
        try:
            async for line in self._response.aiter_lines():
                if not line:
                    if fields:
                        data = fields.get("data")
                        self._frames.put_nowait(
                            Frame(fields.get("event"), fields.get("id"), None if data is None else json.loads(data))
                        )
                    fields = {}
                elif not line.startswith(":"):
                    name, _, value = line.partition(":")
                    fields[name] = value.removeprefix(" ")
        finally:
            self._frames.put_nowait(None)

    async def next(self, within: float = 5.0) -> Frame | None:
        """The next frame, or None once the stream has ended. Raises TimeoutError if none arrives in time."""
        return await asyncio.wait_for(self._frames.get(), within)

    async def take(self, count: int, within: float = 5.0) -> list[Frame]:
        frames: list[Frame] = []
        while len(frames) < count:
            frame = await self.next(within)
            if frame is None:
                break
            frames.append(frame)
        return frames

    async def close(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        await self._response.aclose()


@dataclass(frozen=True)
class Gateway:
    hub: EventHub
    client: httpx.AsyncClient
    readers: list[SseReader]

    async def get(self, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
        return await self.client.get(path, headers=AUTH if headers is None else headers)

    async def stream(self, path: str, headers: dict[str, str] | None = None) -> SseReader:
        request = self.client.build_request("GET", path, headers={**AUTH, **(headers or {})})
        response = await self.client.send(request, stream=True)
        assert response.status_code == 200
        reader = SseReader(response)
        self.readers.append(reader)
        return reader


StartGateway = Callable[..., Awaitable[Gateway]]


@pytest.fixture
async def start_gateway(db: Db, database_url: str) -> AsyncIterator[StartGateway]:
    """Serves the stream routes from a real HTTP server: ASGI test transports buffer the whole response."""
    async with contextlib.AsyncExitStack() as stack:

        async def start(*, listening: bool = True) -> Gateway:
            hub = EventHub()
            if listening:
                listener = listen(hub, database_url, retry_seconds=0.2)
                await listener.start()
                stack.push_async_callback(listener.stop)
            app = FastAPI()
            app.include_router(stream_router(StreamDeps(db, hub, TOKEN)))
            config = uvicorn.Config(
                app, host="127.0.0.1", port=0, log_level="warning", lifespan="off", timeout_graceful_shutdown=1
            )
            server = uvicorn.Server(config)
            task = asyncio.create_task(server.serve())

            async def stop_server() -> None:
                server.should_exit = True
                await task

            stack.push_async_callback(stop_server)
            await until(lambda: server.started)
            port = server.servers[0].sockets[0].getsockname()[1]
            client = await stack.enter_async_context(httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10))
            gateway = Gateway(hub, client, [])

            async def close_readers() -> None:
                for reader in gateway.readers:
                    await reader.close()

            stack.push_async_callback(close_readers)
            return gateway

        yield start


@pytest.fixture
async def gateway(start_gateway: StartGateway) -> Gateway:
    return await start_gateway()


async def until(check: Callable[[], bool], within: float = 5.0) -> None:
    async def checked() -> bool:
        return check()

    await until_async(checked, within)


async def until_async(check: Callable[[], Awaitable[bool]], within: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not await check():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError
        await asyncio.sleep(0.02)


def start_run(db: Db, thread: ThreadKey = THREAD) -> AgentRunRecorder:
    r = AgentRunRecorder(db, "trn-1", thread)
    r.start("triage", "pydantic-ai", "anthropic:claude-opus-5")
    return r


def feed(r: AgentRunRecorder, *events: RunnerEvent) -> None:
    for event in events:
        r.on_runner_event(event)


def delta(value: str, message_id: str) -> AssistantDeltaEvent:
    return AssistantDeltaEvent(at=AT, message_id=message_id, text=value)


def text(value: str, message_id: str) -> AssistantTextEvent:
    return AssistantTextEvent(at=AT, message_id=message_id, text=value)


def thread_path(thread: str) -> str:
    return f"/threads/{quote(thread, safe='')}/stream"


async def note(db: Db, stream: str, thread: ThreadKey, summary: str) -> None:
    async with db.transaction() as tx:
        await append(tx, NewEvent(progressed, stream, "workflow:trn-1", {"summary": summary}, thread))


async def test_a_missing_or_wrong_token_or_another_scheme_is_unauthorized(gateway: Gateway) -> None:
    for headers in ({}, {"authorization": "Bearer not-the-token"}, {"authorization": f"Basic {TOKEN}"}):
        for path in (AGENT_RUN_PATH, THREAD_PATH, thread_path(THREAD), "/agent-runs/run-1/stream"):
            response = await gateway.get(path, headers)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        ("/agent-runs/run-1/stream", {}),
        ("/agent-runs/ar_00000000-0000-0000-0000-00000000000G/stream", {}),
        ("/threads/github/acme/web/discussion/12/stream", {}),
        ("/threads/github/acme/web/issue/012/stream", {}),
        ("/threads/github/acme/we%20b/issue/12/stream", {}),
        (thread_path("acme/web#issue-12"), {}),
        (thread_path("GitHub:acme/web#issue-12"), {}),
        (THREAD_PATH, {"last-event-id": "12"}),
        (thread_path(THREAD), {"last-event-id": "12-"}),
        (f"{AGENT_RUN_PATH}?after=1-2", {}),
        (AGENT_RUN_PATH, {"last-event-id": ""}),
    ],
)
async def test_malformed_ids_threads_and_cursors_are_rejected_before_streaming(
    gateway: Gateway, path: str, headers: dict[str, str]
) -> None:
    assert (await gateway.get(path, {**AUTH, **headers})).status_code == 400


async def test_an_agent_run_stream_sends_the_backlog_then_partials_then_deltas_and_ends_when_the_run_does(
    db: Db, gateway: Gateway
) -> None:
    r = start_run(db)
    feed(r, delta("Hel", "m1"), delta("lo", "m1"), text("Hello", "m1"), delta("Work", "m2"))
    await until(lambda: gateway.hub.partials(r.stream) == [PartialMessage(r.stream, THREAD, "m2", "Work")])

    stream = await gateway.stream(f"/agent-runs/{r.id}/stream")
    started, message, partial = await stream.take(3)
    assert (started.event, started.id) == ("agent_run.started", "1")
    assert {k: started.data[k] for k in ("stream", "stream_version", "thread", "version", "source")} == {
        "stream": r.stream,
        "stream_version": 1,
        "thread": THREAD,
        "version": 1,
        "source": "workflow:trn-1",
    }
    assert datetime.fromisoformat(started.data["created_at"]).tzinfo is not None
    assert (message.event, message.id, message.data["payload"]) == (
        "assistant.message",
        "2",
        {"message_id": "m1", "text": "Hello"},
    )
    assert partial == Frame(
        "live.partial", None, {"stream": r.stream, "thread": THREAD, "message_id": "m2", "text": "Work"}
    )

    feed(r, delta("ing", "m2"))
    assert await stream.next() == Frame(
        "live.delta", None, {"stream": r.stream, "thread": THREAD, "message_id": "m2", "text": "ing"}
    )

    feed(r, text("Working", "m2"))
    await r.finish(SUMMARY)
    assert [(f.event, f.id) for f in await stream.take(3)] == [("assistant.message", "3"), ("agent_run.finished", "4")]
    await until(lambda: gateway.hub.partials(r.stream) == [])


async def test_the_partial_of_a_message_the_backlog_already_finished_is_skipped(db: Db, gateway: Gateway) -> None:
    r = start_run(db)
    feed(r, text("Hello", "m1"), delta("Work", "m2"))
    await until(lambda: [p.message_id for p in gateway.hub.partials(r.stream)] == ["m2"])
    gateway.hub.receive(
        "live",
        f'{{"type":"delta","stream":"{r.stream}","thread":"{THREAD}","message_id":"m1","local_seq":1,"part":0,'
        '"text":"Hello"}',
    )

    stream = await gateway.stream(f"/agent-runs/{r.id}/stream")
    frames = await stream.take(3)
    assert [(f.event, f.data.get("message_id")) for f in frames] == [
        ("agent_run.started", None),
        ("assistant.message", None),
        ("live.partial", "m2"),
    ]
    await r.finish(SUMMARY)
    assert [f.event for f in await stream.take(3)] == ["agent_run.finished"]


async def test_an_agent_run_stream_resumes_after_last_event_id_or_after(db: Db, gateway: Gateway) -> None:
    r = start_run(db)
    feed(r, delta("Hi", "m1"), text("Hi", "m1"), ToolCallEvent(at=AT, tool_call_id="c1", tool="ls", args={}))
    await r.finish(SUMMARY)
    for path, headers in (
        (f"/agent-runs/{r.id}/stream", {"last-event-id": "2"}),
        (f"/agent-runs/{r.id}/stream?after=2", {}),
    ):
        stream = await gateway.stream(path, headers)
        assert [(f.event, f.id) for f in await stream.take(10)] == [("tool.call", "3"), ("agent_run.finished", "4")]


async def test_a_thread_stream_shows_every_stream_on_the_thread_with_deltas_and_resumes_from_tx_seq(
    db: Db, gateway: Gateway
) -> None:
    thread = ThreadKey("github:acme/web#issue-77")
    async with db.transaction() as tx:
        await append(tx, NewEvent(opened, f"thread:{thread}", "workflow:trn-1", {"run_id": "trn-1"}, thread))
    await note(db, "run:trn-1", thread, "triaging")
    await note(db, "run:trn-2", ThreadKey("github:acme/web#issue-78"), "other")
    r = start_run(db, thread)
    feed(r, text("Reading the issue.", "m1"))

    async def recorded() -> bool:
        return len(await db.query("select 1 from events where stream = %s", (r.stream,))) == 2

    await until_async(recorded)

    stream = await gateway.stream(thread_path(thread))
    backlog = await stream.take(4)
    assert [f.event for f in backlog] == ["test.opened", "test.progressed", "agent_run.started", "assistant.message"]
    assert all(f.id is not None and f.id.count("-") == 1 for f in backlog)

    feed(r, delta("Draft", "m2"))
    assert await stream.next() == Frame(
        "live.delta", None, {"stream": r.stream, "thread": thread, "message_id": "m2", "text": "Draft"}
    )
    await r.finish(SUMMARY)
    finished = await stream.next()
    assert finished is not None
    assert finished.event == "agent_run.finished"

    assert backlog[1].id is not None
    resumed = await gateway.stream(thread_path(thread), {"last-event-id": backlog[1].id})
    assert [f.event for f in await resumed.take(3)] == ["agent_run.started", "assistant.message", "agent_run.finished"]


async def test_a_thread_stream_delivers_an_event_whose_transaction_committed_late_in_order(
    db: Db, gateway: Gateway
) -> None:
    stream = await gateway.stream(THREAD_PATH)
    async with db.transaction() as slow:
        await append(slow, NewEvent(progressed, "run:slow", "workflow:slow", {"summary": "slow"}, THREAD))
        await note(db, "run:fast", THREAD, "fast")
        with pytest.raises(TimeoutError):
            await stream.next(within=1.0)
    frames = await stream.take(2)
    assert [f.data["payload"]["summary"] for f in frames] == ["slow", "fast"]
    assert [f.data["seq"] for f in frames] == sorted(f.data["seq"] for f in frames)


async def test_the_github_alias_and_the_key_path_serve_the_same_thread(db: Db, gateway: Gateway) -> None:
    issue = ThreadKey("github:acme/web#issue-55")
    await note(db, "run:trn-4", issue, "hello")
    for path in (thread_path(issue), "/threads/github/acme/web/issue/55/stream"):
        stream = await gateway.stream(path)
        frame = await stream.next()
        assert frame is not None
        assert (frame.event, frame.data["thread"]) == ("test.progressed", issue)


async def test_a_reader_catches_up_on_a_missed_notification_with_the_polling_fallback(
    db: Db, start_gateway: StartGateway
) -> None:
    deaf = await start_gateway(listening=False)
    stream = await deaf.stream(THREAD_PATH)
    await note(db, "run:trn-5", THREAD, "unannounced")
    frame = await stream.next(within=7.0)
    assert frame is not None
    assert frame.event == "test.progressed"


async def test_the_hub_reconnects_after_its_listen_connection_is_killed(db: Db, gateway: Gateway) -> None:
    async def listeners() -> list[int]:
        rows = await db.query(
            "select pid from pg_stat_activity where datname = current_database() and query like 'listen events%'"
        )
        return [row["pid"] for row in rows]

    async def one_listener() -> bool:
        return len(await listeners()) == 1

    await until_async(one_listener)
    [killed] = await listeners()
    await db.query("select pg_terminate_backend(%s)", (killed,))

    async def replaced() -> bool:
        return (pids := await listeners()) != [] and killed not in pids

    await until_async(replaced)

    r = start_run(db)
    stream = await gateway.stream(f"/agent-runs/{r.id}/stream")
    started = await stream.next()
    assert started is not None
    assert started.event == "agent_run.started"
    feed(r, delta("Hello", "m1"))
    assert await stream.next() == Frame(
        "live.delta", None, {"stream": r.stream, "thread": THREAD, "message_id": "m1", "text": "Hello"}
    )
    await r.finish(SUMMARY)
