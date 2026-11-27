"""Tables derived from the event log, updated in the transaction that appends each event."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from psycopg import sql
from psycopg.types.json import Jsonb

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
    def table(self) -> str: ...

    @property
    def key(self) -> Sequence[str]: ...

    @property
    def operational(self) -> Sequence[str]:
        """Columns only the projection holds; a rebuild keeps them."""
        ...

    async def apply(self, tx: Tx, event: LogEvent) -> None: ...


@dataclass(frozen=True)
class _Runs:
    table: str = "runs"
    key: Sequence[str] = ("run_id",)
    operational: Sequence[str] = ()

    async def apply(self, tx: Tx, event: LogEvent) -> None:
        if not event.stream.startswith("run:") and event.type != run_started.type:
            return
        run_id = event.stream.removeprefix("run:")
        if event.type == run_started.type:
            started = run_started.parse(event.payload)
            await tx.query(
                "insert into runs (run_id, trace_id, task, title, thread, state, started_at) "
                "values (%s, %s, %s, %s, %s, 'running', %s)",
                (started.run_id, started.trace_id, started.task, started.title, event.thread, event.created_at),
            )
        elif event.type == run_rejected.type:
            rejected = run_rejected.parse(event.payload)
            await tx.query(
                "insert into runs (run_id, trace_id, task, title, thread, state, outcome, summary, links, "
                "started_at, finished_at) values (%s, %s, %s, %s, %s, 'rejected', 'permission_denied', %s, '[]', "
                "%s, %s)",
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
                "update runs set summary = %s where run_id = %s and state = 'running'", (progress.summary, run_id)
            )
        elif event.type == run_sandbox_attached.type:
            attached = run_sandbox_attached.parse(event.payload)
            await tx.query(
                "update runs set sandbox_id = %s where run_id = %s and state = 'running'",
                (attached.sandbox_id, run_id),
            )
        elif event.type == run_stopped.type:
            stopped = run_stopped.parse(event.payload)
            pending = Jsonb(stopped.unconfirmed.model_dump(mode="json")) if stopped.unconfirmed else None
            await tx.query(
                "update runs set state = 'cancelled', outcome = 'superseded', summary = %s, waiting = null, "
                "cleanup_pending = %s, finished_at = %s where run_id = %s and state = 'running'",
                (stopped.summary, pending, event.created_at, run_id),
            )
        elif event.type == run_awaiting_reply.type:
            await tx.query(
                "update runs set waiting = 'reply' where run_id = (select trace_id from runs where run_id = %s)",
                (run_id,),
            )
        elif event.type == run_cleaned_up.type:
            await tx.query("update runs set cleanup_pending = null where run_id = %s", (run_id,))
        elif event.type == run_finished.type:
            finished = run_finished.parse(event.payload)
            links = json.dumps([link.model_dump(mode="json") for link in finished.links])
            await tx.query(
                "update runs set state = %s, outcome = %s, summary = %s, links = %s::jsonb, waiting = %s, "
                "finished_at = %s where run_id = %s and state = 'running'",
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
    table: str = "threads"
    key: Sequence[str] = ("thread",)
    operational: Sequence[str] = ("status_comment_id",)

    async def apply(self, tx: Tx, event: LogEvent) -> None:
        if event.type != run_started.type:
            return
        started = run_started.parse(event.payload)
        await tx.query(
            "insert into threads (thread, trace_id, run_id) values (%s, %s, %s) "
            "on conflict (thread) do update set trace_id = excluded.trace_id, run_id = excluded.run_id",
            (event.thread, started.trace_id, started.run_id),
        )


PROJECTIONS: tuple[Projection, ...] = (_Runs(), _Threads())


async def project(tx: Tx, event: LogEvent, projections: Sequence[Projection] = PROJECTIONS) -> None:
    for projection in projections:
        await projection.apply(tx, event)


async def rebuild(db: Db, projections: Sequence[Projection] = PROJECTIONS) -> int:
    """Replays the whole log into the projections, keeping their operational columns. Returns the event count."""
    async with db.transaction() as tx:
        await tx.query("lock table events in share mode")
        for projection in projections:
            if projection.operational:
                columns = sql.SQL(", ").join(sql.Identifier(c) for c in (*projection.key, *projection.operational))
                await tx.query(
                    sql.SQL("create temp table {} on commit drop as select {} from {}").format(
                        sql.Identifier(f"keep_{projection.table}"), columns, sql.Identifier(projection.table)
                    )
                )
        if projections:
            tables = sql.SQL(", ").join(sql.Identifier(p.table) for p in projections)
            await tx.query(sql.SQL("truncate {}").format(tables))

        replayed = 0
        after = 0
        while True:
            rows = await tx.query(
                f"select {EVENT_COLUMNS} from events where seq > %s order by seq limit 1000", (after,)
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
            table = sql.Identifier(projection.table)
            keep = sql.Identifier(f"keep_{projection.table}")
            assignments = sql.SQL(", ").join(
                sql.SQL("{} = keep.{}").format(sql.Identifier(c), sql.Identifier(c)) for c in projection.operational
            )
            match = sql.SQL(" and ").join(
                sql.SQL("{}.{} = keep.{}").format(table, sql.Identifier(c), sql.Identifier(c)) for c in projection.key
            )
            await tx.query(sql.SQL("update {} set {} from {} keep where {}").format(table, assignments, keep, match))
        return replayed
