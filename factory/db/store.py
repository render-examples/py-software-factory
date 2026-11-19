"""Appending to the event log."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Generic

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from factory.db.pool import Tx
from factory.db.projections import project
from factory.db.read import EVENT_COLUMNS, LogEvent, to_event
from factory.events.catalog import EventSpec, P, Payload
from factory.threads import ThreadKey, is_thread_key

MAX_PAYLOAD_BYTES: Final = 256 * 1024
EVENTS_CHANNEL: Final = "events"


@dataclass(frozen=True)
class NewEvent(Generic[P]):
    type: EventSpec[P]
    stream: str
    source: str
    payload: P | Mapping[str, Any]
    thread: ThreadKey | None = None
    idempotency_key: str | None = None


class VersionConflict(Exception):
    def __init__(self, stream: str) -> None:
        super().__init__(f"stream {stream} is not at the expected version")
        self.stream = stream


def encode_payload(event: NewEvent[Any]) -> dict[str, Any]:
    """The validated payload as stored. Raises ValueError for a payload append would reject."""
    raw = event.payload
    data: object = raw.model_dump(mode="json") if isinstance(raw, Payload) else raw
    payload = event.type.parse(data).model_dump(mode="json", exclude_none=True)
    if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"{event.type.type} payload is larger than 256 KB")
    if event.thread is not None and not is_thread_key(event.thread):
        raise ValueError(f"{event.type.type} has an invalid thread key")
    return payload


_INSERT: Final = (
    "insert into events (stream, stream_version, thread, type, version, source, idempotency_key, payload) "
    "select %(stream)s::text, coalesce(max(stream_version), 0) + 1, %(thread)s::text, %(type)s::text, "
    "%(version)s::int, %(source)s::text, %(key)s::text, %(payload)s::jsonb "
    "from events where stream = %(stream)s::text "
    "having %(expected)s::int is null or coalesce(max(stream_version), 0) = %(expected)s::int "
    "on conflict (idempotency_key) do nothing "
    f"returning {EVENT_COLUMNS}"
)


async def append(tx: Tx, event: NewEvent[Any], *, expected_version: int | None = None) -> LogEvent | None:
    """Appends one event, updates the projections, and notifies listeners, all in `tx`.

    Returns None when the idempotency key was already used. Raises VersionConflict when the stream isn't at
    `expected_version`; the transaction is then aborted and must be retried from the start.
    """
    payload = encode_payload(event)
    params = {
        "stream": event.stream,
        "thread": event.thread,
        "type": event.type.type,
        "version": event.type.version,
        "source": event.source,
        "key": event.idempotency_key,
        "payload": Jsonb(payload),
        "expected": expected_version,
    }
    try:
        rows = await tx.query(_INSERT, params)
    except UniqueViolation as error:
        raise VersionConflict(event.stream) from error
    if not rows:
        if event.idempotency_key is not None and await tx.query(
            "select 1 from events where idempotency_key = %s", (event.idempotency_key,)
        ):
            return None
        raise VersionConflict(event.stream)
    logged = to_event(rows[0])
    await project(tx, logged)
    await tx.query("select pg_notify(%s, %s)", (EVENTS_CHANNEL, f"{logged.stream} {logged.stream_version}"))
    return logged


async def append_many(
    tx: Tx, stream: str, events: Sequence[NewEvent[Any]], *, expected_version: int | None = None
) -> list[LogEvent]:
    """Appends events to one stream in order; each after the first expects the version the previous one wrote."""
    appended: list[LogEvent] = []
    for event in events:
        logged = await append(
            tx,
            NewEvent(event.type, stream, event.source, event.payload, event.thread, event.idempotency_key),
            expected_version=expected_version,
        )
        if logged is None:
            continue
        appended.append(logged)
        expected_version = logged.stream_version
    return appended
