"""Ownership of a thread. A run owns its issue or PR by starting on it: the trace whose run started last owns the
thread until its runs finish."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from factory.db import Db, NewEvent, Queryable, VersionConflict, append
from factory.events import EventSpec, Payload, run_cleaned_up, run_started, run_stopped, warn
from factory.events.catalog import RunMode, Unconfirmed
from factory.runs.run_control import RunControl, RunNotFoundError
from factory.runs.types import Cleanup
from factory.threads import ThreadKey

_ACTIVE_STATUSES = frozenset({"pending", "running", "paused"})
"""Render reports a parent waiting on a subtask as paused; it is still active."""

START_ATTEMPTS = 4


def run_event(
    spec: EventSpec[Any], run_id: str, thread: ThreadKey, payload: Payload, by: str | None = None
) -> NewEvent[Any]:
    return NewEvent(spec, f"run:{run_id}", f"workflow:{by or run_id}", payload, thread)


@dataclass(frozen=True)
class StartingRun:
    thread: ThreadKey
    run_id: str
    trace_id: str
    task: str
    title: str
    actor: str
    mode: RunMode
    reply_to: str | None = None


@dataclass(frozen=True)
class Started:
    displaced: str | None
    """The trace this run took the thread from, if any."""


async def start_on_thread(db: Db, run: StartingRun) -> Started | None:
    """Appends `run.started` to the thread at the version it read. None when the run may not start: a child whose
    trace no longer owns the thread, or a reply to a run that is no longer the thread's latest or no longer waiting."""
    stream = f"thread:{run.thread}"
    for _ in range(START_ATTEMPTS):
        try:
            async with db.transaction() as tx:
                rows = await tx.query(
                    "select (select trace_id from threads where thread = $1) as trace_id, "
                    "(select coalesce(max(stream_version), 0) from events where stream = $2) as version, "
                    "(select waiting from runs where run_id = $3) as reply_waiting",
                    (run.thread, stream, run.reply_to or ""),
                )
                latest: str | None = rows[0]["trace_id"]
                if run.mode == "continue" and latest != run.trace_id:
                    return None
                if run.mode == "reply" and (latest != run.reply_to or rows[0]["reply_waiting"] != "reply"):
                    return None
                payload = run_started.payload(
                    run_id=run.run_id,
                    trace_id=run.trace_id,
                    task=run.task,
                    title=run.title,
                    actor=run.actor,
                    mode=run.mode,
                )
                event = NewEvent(run_started, stream, f"workflow:{run.run_id}", payload, run.thread)
                await append(tx, event, expected_version=int(rows[0]["version"]))
                return Started(None if latest == run.trace_id else latest)
        except VersionConflict:
            continue
    return None


async def is_current(db: Queryable, thread: ThreadKey, trace_id: str, run_id: str) -> bool:
    rows = await db.query(
        "select 1 from threads t join runs r on r.run_id = $1 "
        "where t.thread = $2 and t.trace_id = $3 and r.state = 'running'",
        (run_id, thread, trace_id),
    )
    return bool(rows)


@dataclass(frozen=True)
class Stopped:
    run_ids: tuple[str, ...]
    unconfirmed: tuple[str, ...]


async def stop_trace(db: Db, runs: RunControl, trace_id: str, *, by: str, summary: str) -> Stopped:
    """Stops a displaced trace: cancels its task run if it's active, terminates the sandboxes its running runs
    recorded, and records `run.stopped` for each. Render kills a cancelled run without running its cleanup, so this
    is the only place its sandboxes get terminated and its end gets recorded."""
    rows = await db.query(
        "select run_id, thread, sandbox_id from runs where trace_id = $1 and state = 'running'", (trace_id,)
    )
    if not rows:
        return Stopped((), ())
    cancel = await cancel_if_active(runs, trace_id)
    failed_sandboxes: set[str] = set()
    for row in rows:
        sandbox_id = row["sandbox_id"]
        if sandbox_id and not await terminate(runs, sandbox_id):
            failed_sandboxes.add(sandbox_id)
    async with db.transaction() as tx:
        for row in rows:
            task_run = trace_id if cancel == "failed" and row["run_id"] == trace_id else None
            sandboxes = (row["sandbox_id"],) if row["sandbox_id"] in failed_sandboxes else ()
            unconfirmed = Unconfirmed(task_run=task_run, sandboxes=sandboxes) if task_run or sandboxes else None
            payload = run_stopped.payload(by=by, active=cancel != "inactive", summary=summary, unconfirmed=unconfirmed)
            await append(tx, run_event(run_stopped, row["run_id"], row["thread"], payload, by))
    unconfirmed_ids = ((trace_id,) if cancel == "failed" else ()) + tuple(sorted(failed_sandboxes))
    return Stopped(tuple(row["run_id"] for row in rows), unconfirmed_ids)


async def retry_cleanup(db: Db, control: Callable[[], RunControl], thread: ThreadKey, by: str) -> Cleanup:
    """Retries stopping what earlier runs on the thread couldn't confirm, and records each success."""
    rows = await db.query(
        "select run_id, cleanup_pending from runs where thread = $1 and cleanup_pending is not null", (thread,)
    )
    confirmed: list[str] = []
    unconfirmed: list[str] = []
    for row in rows:
        pending = Unconfirmed.model_validate(row["cleanup_pending"])
        runs = control()
        resources = [*([pending.task_run] if pending.task_run else []), *pending.sandboxes]
        stopped = pending.task_run is None or await cancel_if_active(runs, pending.task_run) != "failed"
        for sandbox_id in pending.sandboxes:
            stopped = await terminate(runs, sandbox_id) and stopped
        if stopped:
            async with db.transaction() as tx:
                await append(tx, run_event(run_cleaned_up, row["run_id"], thread, run_cleaned_up.payload(by=by), by))
            confirmed.extend(resources)
        else:
            unconfirmed.extend(resources)
    return Cleanup(tuple(confirmed), tuple(unconfirmed))


async def cancel_if_active(runs: RunControl, task_run_id: str) -> Literal["stopped", "inactive", "failed"]:
    """A confirmed 404 or a finished run is inactive. A run whose status can't be read is cancelled anyway."""
    try:
        if await runs.get_status(task_run_id) not in _ACTIVE_STATUSES:
            return "inactive"
    except RunNotFoundError:
        return "inactive"
    except Exception as error:
        warn("could not read the task run; cancelling it anyway", task_run_id=task_run_id, error=str(error))
    try:
        await runs.cancel(task_run_id)
    except Exception as error:
        warn("could not cancel the stopped run", task_run_id=task_run_id, error=str(error))
        return "failed"
    return "stopped"


async def terminate(runs: RunControl, sandbox_id: str) -> bool:
    try:
        await runs.terminate_sandbox(sandbox_id)
    except Exception as error:
        warn("could not terminate the stopped run's sandbox", sandbox_id=sandbox_id, error=str(error))
        return False
    return True
