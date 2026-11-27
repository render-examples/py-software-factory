"""Appending to the event log."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Generic

from asyncpg import UniqueViolationError

from factory.db.pool import Tx
from factory.db.projections import project
from factory.db.read import EVENT_COLUMNS, LogEvent, to_event
from factory.events.catalog import EventSpec, P, Payload
from factory.threads import ThreadKey, is_thread_key

MAX_PAYLOAD_BYTES: Final = 256 * 1024


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
    "select $1::text, coalesce(max(stream_version), 0) + 1, $2::text, $3::text, $4::int, $5::text, $6::text, "
    "$7::jsonb from events where stream = $1::text "
    "having $8::int is null or coalesce(max(stream_version), 0) = $8::int "
    "on conflict (idempotency_key) do nothing "
    f"returning {EVENT_COLUMNS}"
)


async def append(tx: Tx, event: NewEvent[Any], *, expected_version: int | None = None) -> LogEvent | None:
    """Appends one event and updates the projections, in `tx`.

    Returns None when the idempotency key was already used. Raises VersionConflict when the stream isn't at
    `expected_version`; the transaction is then aborted and must be retried from the start.
    """
    payload = encode_payload(event)
    params = (
        event.stream,
        event.thread,
        event.type.type,
        event.type.version,
        event.source,
        event.idempotency_key,
        payload,
        expected_version,
    )
    try:
        rows = await tx.query(_INSERT, params)
    except UniqueViolationError as error:
        raise VersionConflict(event.stream) from error
    if not rows:
        if event.idempotency_key is not None and await tx.query(
            "select 1 from events where idempotency_key = $1", (event.idempotency_key,)
        ):
            return None
        raise VersionConflict(event.stream)
    logged = to_event(rows[0])
    await project(tx, logged)
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
