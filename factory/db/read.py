from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from factory.db.pool import Queryable, Row
from factory.threads import ThreadKey


@dataclass(frozen=True)
class LogEvent:
    seq: int
    tx: str
    stream: str
    stream_version: int
    thread: ThreadKey | None
    type: str
    version: int
    source: str
    payload: Any
    created_at: datetime


@dataclass(frozen=True)
class ThreadCursor:
    tx: str
    seq: int


START: Final = ThreadCursor("0", 0)

EVENT_COLUMNS: Final = "seq, tx::text as tx, stream, stream_version, thread, type, version, source, payload, created_at"


def to_event(row: Row) -> LogEvent:
    return LogEvent(
        seq=int(row["seq"]),
        tx=str(row["tx"]),
        stream=row["stream"],
        stream_version=int(row["stream_version"]),
        thread=row["thread"],
        type=row["type"],
        version=int(row["version"]),
        source=row["source"],
        payload=row["payload"],
        created_at=row["created_at"],
    )


async def read_stream(db: Queryable, stream: str, after_version: int, limit: int = 500) -> list[LogEvent]:
    rows = await db.query(
        f"select {EVENT_COLUMNS} from events where stream = %s and stream_version > %s "
        "order by stream_version limit %s",
        (stream, after_version, limit),
    )
    return [to_event(row) for row in rows]


async def read_thread(db: Queryable, thread: ThreadKey, after: ThreadCursor, limit: int = 500) -> list[LogEvent]:
    """A thread's events in commit-safe order: nothing at or above the oldest open transaction is returned, so a
    slow transaction's event is never skipped by a reader that saw a faster, later one."""
    rows = await db.query(
        f"select {EVENT_COLUMNS} from events "
        "where thread = %s and (tx, seq) > (%s::xid8, %s::bigint) and tx < pg_snapshot_xmin(pg_current_snapshot()) "
        "order by tx, seq limit %s",
        (thread, after.tx, after.seq, limit),
    )
    return [to_event(row) for row in rows]
