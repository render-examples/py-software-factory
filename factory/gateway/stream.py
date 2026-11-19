"""Server-Sent Event streams of the event log: one agent run, or every stream on an issue or PR thread.

Each stream sends the backlog after the client's cursor, then the messages still being written, then live events
and token deltas. Readers hold `EVENTS_READ_TOKEN`, which can read every thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sse_starlette import EventSourceResponse, ServerSentEvent

from factory.db import Db, LogEvent, ThreadCursor, read_stream, read_thread
from factory.events import LiveDelta, agent_run_finished, assistant_message, warn
from factory.gateway.hub import ANY_STREAM, EventHub
from factory.gateway.live import PartialMessage
from factory.threads import GitHubThread, ThreadKey, github_thread, is_thread_key

PAGE = 500
POLL_SECONDS = 5.0
KEEPALIVE_SECONDS = 15

_AGENT_RUN_ID = re.compile(r"^ar_[0-9a-f-]{36}$")
_NAME = re.compile(r"^[\w.-]{1,100}$", re.ASCII)
_NUMBER = re.compile(r"^[1-9][0-9]{0,9}$")
_BEARER = re.compile(r"^Bearer (.+)$")
_VERSION_CURSOR = re.compile(r"^([0-9]{1,10})$")
_THREAD_CURSOR = re.compile(r"^([0-9]{1,20})-([0-9]{1,15})$")


@dataclass(frozen=True)
class StreamDeps:
    db: Db
    hub: EventHub
    events_read_token: str


def stream_router(deps: StreamDeps) -> APIRouter:
    router = APIRouter()
    expected = _digest(deps.events_read_token)

    @router.get("/agent-runs/{agent_run_id}/stream")
    async def agent_run(request: Request, agent_run_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not _authorized(request, expected):
            return _unauthorized()
        after = _cursor(request, _VERSION_CURSOR)
        if not _AGENT_RUN_ID.fullmatch(agent_run_id) or after is None:
            return JSONResponse({"error": "invalid agent run ID or cursor"}, status_code=400)
        return _agent_run_stream(deps, f"agent_run:{agent_run_id}", int(after[0]) if after else 0)

    # Registered before the generic thread route, whose `path` parameter would otherwise match the alias.
    @router.get("/threads/github/{owner}/{repo}/{kind}/{number}/stream")
    async def github(  # pyright: ignore[reportUnusedFunction]
        request: Request, owner: str, repo: str, kind: str, number: str
    ) -> Response:
        if not _authorized(request, expected):
            return _unauthorized()
        valid = _NAME.fullmatch(owner) and _NAME.fullmatch(repo) and _NUMBER.fullmatch(number)
        if not valid or kind not in ("issue", "pr"):
            return _invalid_thread()
        github_kind: Literal["issue", "pr"] = "issue" if kind == "issue" else "pr"
        return _serve_thread(request, deps, github_thread(f"{owner}/{repo}", GitHubThread(github_kind, int(number))))

    @router.get("/threads/{thread:path}/stream")
    async def thread(request: Request, thread: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not _authorized(request, expected):
            return _unauthorized()
        if not is_thread_key(thread):
            return _invalid_thread()
        return _serve_thread(request, deps, ThreadKey(thread))

    return router


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _authorized(request: Request, expected: bytes) -> bool:
    given = _BEARER.fullmatch(request.headers.get("authorization", ""))
    return given is not None and hmac.compare_digest(_digest(given[1]), expected)


def _unauthorized() -> Response:
    return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})


def _invalid_thread() -> Response:
    return JSONResponse({"error": "invalid thread or cursor"}, status_code=400)


def _cursor(request: Request, pattern: re.Pattern[str]) -> tuple[str, ...] | None:
    """The client's position: () to start from the beginning, or None when it's malformed."""
    value = request.headers.get("last-event-id")
    if value is None:
        value = request.query_params.get("after")
    if value is None:
        return ()
    match = pattern.fullmatch(value)
    return match.groups() if match else None


def _agent_run_stream(deps: StreamDeps, stream: str, after: int) -> Response:
    version = after

    async def read() -> list[tuple[str, LogEvent]]:
        nonlocal version
        events = await read_stream(deps.db, stream, version, PAGE)
        if events:
            version = events[-1].stream_version
        return [(str(event.stream_version), event) for event in events]

    feed = _Feed(key=stream, wake_on=stream, read=read, ends=lambda event: event.type == agent_run_finished.type)
    return _respond(deps.hub, feed)


def _serve_thread(request: Request, deps: StreamDeps, thread: ThreadKey) -> Response:
    after = _cursor(request, _THREAD_CURSOR)
    if after is None:
        return _invalid_thread()
    position = ThreadCursor(after[0], int(after[1])) if after else ThreadCursor("0", 0)

    async def read() -> list[tuple[str, LogEvent]]:
        nonlocal position
        events = await read_thread(deps.db, thread, position, PAGE)
        if events:
            position = ThreadCursor(events[-1].tx, events[-1].seq)
        return [(f"{event.tx}-{event.seq}", event) for event in events]

    return _respond(deps.hub, _Feed(key=thread, wake_on=ANY_STREAM, read=read))


@dataclass(frozen=True)
class _Feed:
    key: str
    """The stream or thread whose partials and deltas the reader sees."""
    wake_on: str
    read: Callable[[], Awaitable[list[tuple[str, LogEvent]]]]
    """The next page after the last one read, each event with its SSE ID."""
    ends: Callable[[LogEvent], bool] = lambda _event: False


def _respond(hub: EventHub, feed: _Feed) -> Response:
    return EventSourceResponse(
        _frames(hub, feed),
        ping=KEEPALIVE_SECONDS,
        ping_message_factory=lambda: ServerSentEvent(comment="keepalive"),
    )


_Work = ServerSentEvent | Literal["drain", "partials"]


async def _frames(hub: EventHub, feed: _Feed) -> AsyncIterator[ServerSentEvent]:
    """Every write goes through one queue, so backlog pages, partials and deltas reach the client in order."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[_Work] = asyncio.Queue()
    finished: set[tuple[str, str]] = set()
    live = False
    woken = False

    def wake() -> None:
        nonlocal woken
        if not woken:
            woken = True
            queue.put_nowait("drain")

    def on_delta(delta: LiveDelta) -> None:
        if live:
            queue.put_nowait(ServerSentEvent(_text_json(delta), event="live.delta"))

    unsubscribe = hub.on_events(feed.wake_on, wake)
    unsubscribe_deltas = hub.on_delta(feed.key, on_delta)
    try:
        wake()
        queue.put_nowait("partials")
        next_poll = loop.time() + POLL_SECONDS
        while True:
            try:
                work = await asyncio.wait_for(queue.get(), timeout=max(0.0, next_poll - loop.time()))
            except TimeoutError:
                next_poll = loop.time() + POLL_SECONDS
                wake()
                continue
            if work == "drain":
                woken = False
                while True:
                    page = await feed.read()
                    for cursor, event in page:
                        yield ServerSentEvent(_event_json(event), event=event.type, id=cursor)
                        if event.type == assistant_message.type:
                            finished.add((event.stream, assistant_message.parse(event.payload).message_id))
                        if feed.ends(event):
                            return
                    if len(page) < PAGE:
                        break
            elif work == "partials":
                live = True
                for partial in hub.partials(feed.key):
                    if (partial.stream, partial.message_id) not in finished:
                        yield ServerSentEvent(_text_json(partial), event="live.partial")
            else:
                yield work
    except Exception as error:
        # Ending the response lets the client reconnect from its last event ID.
        warn("event stream closed after an error", key=feed.key, error=str(error))
    finally:
        unsubscribe()
        unsubscribe_deltas()


def _event_json(event: LogEvent) -> str:
    return json.dumps(
        {
            "seq": event.seq,
            "tx": event.tx,
            "stream": event.stream,
            "stream_version": event.stream_version,
            "thread": event.thread,
            "type": event.type,
            "version": event.version,
            "source": event.source,
            "payload": event.payload,
            "created_at": event.created_at.isoformat(),
        },
        ensure_ascii=False,
    )


def _text_json(text: PartialMessage | LiveDelta) -> str:
    return json.dumps(
        {"stream": text.stream, "thread": text.thread, "message_id": text.message_id, "text": text.text},
        ensure_ascii=False,
    )
