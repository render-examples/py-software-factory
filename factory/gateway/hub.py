"""Fans Postgres notifications out to the gateway's stream readers, over one `LISTEN` connection per instance."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import TypeVar

import psycopg
from pydantic import ValidationError

from factory.events import LIVE_CHANNEL, LIVE_NOTICE_ADAPTER, LiveDelta, warn
from factory.gateway.live import LiveCache, PartialMessage

ANY_STREAM = "*"
RECONNECT_SECONDS = 5.0
CONNECT_TIMEOUT_SECONDS = 10

_T = TypeVar("_T")


class EventHub:
    def __init__(self) -> None:
        self._events: dict[str, set[Callable[[], None]]] = {}
        self._deltas: dict[str, set[Callable[[LiveDelta], None]]] = {}
        self._cache = LiveCache()

    def on_events(self, stream: str, listener: Callable[[], None]) -> Callable[[], None]:
        """Calls `listener` whenever an event is appended to `stream`, or to any stream for `ANY_STREAM`.
        Returns a function that unsubscribes it."""
        return _add(self._events, stream, listener)

    def on_delta(self, key: str, listener: Callable[[LiveDelta], None]) -> Callable[[], None]:
        """Calls `listener` with each new delta on a stream or a thread. Returns a function that unsubscribes it."""
        return _add(self._deltas, key, listener)

    def partials(self, key: str) -> list[PartialMessage]:
        return self._cache.partials(key)

    def receive(self, channel: str, payload: str) -> None:
        if channel == LIVE_CHANNEL:
            self._receive_live(payload)
            return
        stream = payload.split(" ")[0]
        for listener in [*self._events.get(stream, ()), *self._events.get(ANY_STREAM, ())]:
            listener()

    def _receive_live(self, payload: str) -> None:
        try:
            notice = LIVE_NOTICE_ADAPTER.validate_json(payload)
        except ValidationError:
            return
        delta = self._cache.apply(notice)
        if delta is None:
            return
        by_thread = self._deltas.get(delta.thread, ()) if delta.thread is not None else ()
        for listener in [*self._deltas.get(delta.stream, ()), *by_thread]:
            listener(delta)


class Listener:
    """Feeds a hub from `LISTEN events; LISTEN live`, reconnecting after `retry_seconds` whenever the
    connection fails. Events missed while disconnected reach readers through their polling fallback."""

    def __init__(self, hub: EventHub, url: str, *, retry_seconds: float = RECONNECT_SECONDS) -> None:
        self._hub = hub
        self._url = url
        self._retry_seconds = retry_seconds
        self._task: asyncio.Task[None] | None = None
        self._attempted = asyncio.Event()

    async def start(self) -> None:
        """Returns once the first connection attempt has listened or failed. Never raises for a failed attempt."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        await self._attempted.wait()

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._url, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
                ) as connection:
                    await connection.execute("listen events; listen live")
                    self._attempted.set()
                    async for notice in connection.notifies():
                        self._hub.receive(notice.channel, notice.payload)
                error = "connection ended"
            except Exception as failure:
                error = str(failure)
            self._attempted.set()
            warn("event listener disconnected; reconnecting", error=error)
            await asyncio.sleep(self._retry_seconds)


def listen(hub: EventHub, url: str, *, retry_seconds: float = RECONNECT_SECONDS) -> Listener:
    """A listener for `hub`; call `start()` in the app's lifespan and `stop()` when it shuts down."""
    return Listener(hub, url, retry_seconds=retry_seconds)


def _add(listeners: dict[str, set[_T]], key: str, listener: _T) -> Callable[[], None]:
    subscribed = listeners.setdefault(key, set())
    subscribed.add(listener)

    def remove() -> None:
        subscribed.discard(listener)
        if not subscribed and listeners.get(key) is subscribed:
            del listeners[key]

    return remove
