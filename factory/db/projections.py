"""Tables derived from the event log, updated in the transaction that appends each event."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import LiteralString, Protocol

from factory.db.pool import Db, Tx
from factory.db.read import EVENT_COLUMNS, LogEvent, to_event
from factory.events.catalog import (
    run_awaiting_reply,
    run_cleaned_up,
    run_finished,
    run_progress,
    run_rejected,
    run_sandbox_attached,
    run_started,
    run_stopped,
)


class Projection(Protocol):
    @property
    def table(self) -> LiteralString: ...

    @property
    def key(self) -> Sequence[LiteralString]: ...

    @property
    def operational(self) -> Sequence[LiteralString]:
        """Columns only the projection holds; a rebuild keeps them."""
        ...

    async def apply(self, tx: Tx, event: LogEvent) -> None: ...


@dataclass(frozen=True)
class _Runs:
    table: LiteralString = "runs"
    key: Sequence[LiteralString] = ("run_id",)
    operational: Sequence[LiteralString] = ()

    async def apply(self, tx: Tx, event: LogEvent) -> None:
        if not event.stream.startswith("run:") and event.type != run_started.type:
            return
        run_id = event.stream.removeprefix("run:")
        if event.type == run_started.type:
            started = run_started.parse(event.payload)
            await tx.query(
                "insert into runs (run_id, trace_id, task, title, thread, state, started_at) "
                "values ($1, $2, $3, $4, $5, 'running', $6)",
                (started.run_id, started.trace_id, started.task, started.title, event.thread, event.created_at),
            )
        elif event.type == run_rejected.type:
            rejected = run_rejected.parse(event.payload)
            await tx.query(
                "insert into runs (run_id, trace_id, task, title, thread, state, outcome, summary, links, "
                "started_at, finished_at) values ($1, $2, $3, $4, $5, 'rejected', 'permission_denied', $6, '[]', "
                "$7, $8)",
                (
                    run_id,
                    rejected.trace_id,
                    rejected.task,
                    rejected.title,
                    event.thread,
                    rejected.reason,
                    event.created_at,
                    event.created_at,
                ),
            )
        elif event.type == run_progress.type:
            progress = run_progress.parse(event.payload)
            await tx.query(
                "update runs set summary = $1 where run_id = $2 and state = 'running'", (progress.summary, run_id)
            )
        elif event.type == run_sandbox_attached.type:
            attached = run_sandbox_attached.parse(event.payload)
            await tx.query(
                "update runs set sandbox_id = $1 where run_id = $2 and state = 'running'",
                (attached.sandbox_id, run_id),
            )
        elif event.type == run_stopped.type:
            stopped = run_stopped.parse(event.payload)
            pending = stopped.unconfirmed.model_dump(mode="json") if stopped.unconfirmed else None
            await tx.query(
                "update runs set state = 'cancelled', outcome = 'superseded', summary = $1, waiting = null, "
                "cleanup_pending = $2, finished_at = $3 where run_id = $4 and state = 'running'",
                (stopped.summary, pending, event.created_at, run_id),
            )
        elif event.type == run_awaiting_reply.type:
            await tx.query(
                "update runs set waiting = 'reply' where run_id = (select trace_id from runs where run_id = $1)",
                (run_id,),
            )
        elif event.type == run_cleaned_up.type:
            await tx.query("update runs set cleanup_pending = null where run_id = $1", (run_id,))
        elif event.type == run_finished.type:
            finished = run_finished.parse(event.payload)
            links = [link.model_dump(mode="json") for link in finished.links]
            await tx.query(
                "update runs set state = $1, outcome = $2, summary = $3, links = $4::jsonb, waiting = $5, "
                "finished_at = $6 where run_id = $7 and state = 'running'",
                (
                    finished.state,
                    finished.outcome,
                    finished.summary,
                    links,
                    finished.waiting,
                    event.created_at,
                    run_id,
                ),
            )


@dataclass(frozen=True)
class _Threads:
    table: LiteralString = "threads"
    key: Sequence[LiteralString] = ("thread",)
    operational: Sequence[LiteralString] = ("status_comment_id",)

    async def apply(self, tx: Tx, event: LogEvent) -> None:
        if event.type != run_started.type:
            return
        started = run_started.parse(event.payload)
        await tx.query(
            "insert into threads (thread, trace_id, run_id) values ($1, $2, $3) "
            "on conflict (thread) do update set trace_id = excluded.trace_id, run_id = excluded.run_id",
            (event.thread, started.trace_id, started.run_id),
        )


PROJECTIONS: tuple[Projection, ...] = (_Runs(), _Threads())


async def project(tx: Tx, event: LogEvent, projections: Sequence[Projection] = PROJECTIONS) -> None:
    for projection in projections:
        await projection.apply(tx, event)


_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def _quoted(name: LiteralString) -> LiteralString:
    """A projection's own table or column name, quoted. Names are code constants; this guards against a typo."""
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"not a plain SQL identifier: {name!r}")
    return f'"{name}"'


async def rebuild(db: Db, projections: Sequence[Projection] = PROJECTIONS) -> int:
    """Replays the whole log into the projections, keeping their operational columns. Returns the event count."""
    async with db.transaction() as tx:
        await tx.query("lock table events in share mode")
        for projection in projections:
            if projection.operational:
                columns = ", ".join(_quoted(c) for c in (*projection.key, *projection.operational))
                keep = _quoted(f"keep_{projection.table}")
                await tx.query(
                    f"create temp table {keep} on commit drop as select {columns} from {_quoted(projection.table)}"
                )
        if projections:
            await tx.query(f"truncate {', '.join(_quoted(p.table) for p in projections)}")

        replayed = 0
        after = 0
        while True:
            rows = await tx.query(
                f"select {EVENT_COLUMNS} from events where seq > $1 order by seq limit 1000", (after,)
            )
            if not rows:
                break
            for event in map(to_event, rows):
                await project(tx, event, projections)
                after = event.seq
            replayed += len(rows)

        for projection in projections:
            if not projection.operational:
                continue
            table = _quoted(projection.table)
            keep = _quoted(f"keep_{projection.table}")
            assignments = ", ".join(f"{_quoted(c)} = keep.{_quoted(c)}" for c in projection.operational)
            match = " and ".join(f"{table}.{_quoted(c)} = keep.{_quoted(c)}" for c in projection.key)
            await tx.query(f"update {table} set {assignments} from {keep} keep where {match}")
        return replayed
