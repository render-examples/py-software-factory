from __future__ import annotations

import asyncio
import uuid
from typing import Any

import logfire
import pytest
from logfire.testing import CaptureLogfire

from factory.db import (
    Db,
    NewEvent,
    VersionConflict,
    append,
    append_many,
    create_db,
    migrate,
    migration_names,
    pending_migrations,
    read_stream,
    rebuild,
)
from factory.events import Payload, define_event, run_finished, run_progress, run_started
from factory.threads import ThreadKey
from tests.db import temporary_database

pytestmark = pytest.mark.postgres


class Noted(Payload):
    text: str


noted = define_event("test.noted", 1, Noted)
THREAD = ThreadKey("github:acme/web#issue-12")


def new_stream() -> str:
    return f"run:{uuid.uuid4()}"


def note(stream: str, text: str, *, thread: ThreadKey | None = None, key: str | None = None) -> NewEvent[Any]:
    return NewEvent(noted, stream, "gateway", {"text": text}, thread, key)


async def test_append_records_the_event_and_numbers_each_stream_from_one(db: Db) -> None:
    stream, other = new_stream(), new_stream()
    async with db.transaction() as tx:
        logged = [
            await append(tx, note(stream, "hello", thread=THREAD)),
            await append(tx, note(other, "1")),
            await append(tx, note(stream, "2")),
            await append(tx, note(stream, "3")),
        ]
    first = logged[0]
    assert first is not None
    assert (first.stream, first.type, first.version, first.source, first.thread, first.payload) == (
        stream,
        "test.noted",
        1,
        "gateway",
        THREAD,
        {"text": "hello"},
    )
    assert [(e.stream, e.stream_version) for e in logged if e] == [(stream, 1), (other, 1), (stream, 2), (stream, 3)]
    assert await read_stream(db, stream, 0, 2) == [logged[0], logged[2]]
    assert await read_stream(db, stream, 2, 2) == [logged[3]]


async def test_append_at_another_version_raises_version_conflict(db: Db) -> None:
    stream = new_stream()
    async with db.transaction() as tx:
        await append(tx, note(stream, "first"), expected_version=0)
    for expected in (0, 2):
        with pytest.raises(VersionConflict):
            async with db.transaction() as tx:
                await append(tx, note(stream, "stale"), expected_version=expected)
    async with db.transaction() as tx:
        await append(tx, note(stream, "second"), expected_version=1)
    assert [e.payload for e in await read_stream(db, stream, 0)] == [{"text": "first"}, {"text": "second"}]


async def test_a_repeated_idempotency_key_changes_nothing(db: Db) -> None:
    stream, key = new_stream(), str(uuid.uuid4())
    async with db.transaction() as tx:
        assert await append(tx, note(stream, "once", key=key)) is not None
        await append(tx, note(stream, "later"))
    async with db.transaction() as tx:
        assert await append(tx, note(stream, "again", key=key), expected_version=0) is None
    assert [e.payload for e in await read_stream(db, stream, 0)] == [{"text": "once"}, {"text": "later"}]


async def test_append_rejects_oversized_payloads_and_bad_thread_keys_before_writing(db: Db) -> None:
    stream = new_stream()
    with pytest.raises(ValueError, match="256 KB"):
        async with db.transaction() as tx:
            await append(tx, note(stream, "é" * 140_000))
    with pytest.raises(ValueError, match="thread key"):
        async with db.transaction() as tx:
            await append(tx, note(stream, "x", thread=ThreadKey("not a key")))
    assert await read_stream(db, stream, 0) == []


async def test_append_many_chains_expected_versions(db: Db) -> None:
    stream = new_stream()
    async with db.transaction() as tx:
        appended = await append_many(tx, stream, [note("", "a"), note("", "b")], expected_version=0)
    assert [e.stream_version for e in appended] == [1, 2]
    with pytest.raises(VersionConflict):
        async with db.transaction() as tx:
            await append_many(tx, stream, [note("", "c")], expected_version=1)


async def test_concurrent_appends_at_one_expected_version_have_exactly_one_winner(db: Db) -> None:
    stream = new_stream()

    async def attempt(text: str) -> str:
        try:
            async with db.transaction() as tx:
                await append(tx, note(stream, text), expected_version=0)
        except VersionConflict:
            return "conflict"
        return "won"

    results = await asyncio.gather(attempt("a"), attempt("b"))
    assert sorted(results) == ["conflict", "won"]
    assert len(await read_stream(db, stream, 0)) == 1


async def test_migrate_applies_each_file_once_even_when_run_concurrently() -> None:
    with temporary_database() as url:
        first, second = create_db(url, 1), create_db(url, 1)
        try:
            assert await pending_migrations(first) == migration_names()
            applied = await asyncio.gather(migrate(first), migrate(second))
            assert sorted(applied, key=len) == [[], migration_names()]
            assert await pending_migrations(first) == []
            assert await migrate(first) == []
        finally:
            await first.aclose()
            await second.aclose()


async def test_rebuild_replays_projections_and_keeps_operational_columns(db: Db) -> None:
    run_id, trace_id = "run-1", "run-1"
    async with db.transaction() as tx:
        await append(
            tx,
            NewEvent(
                run_started,
                f"thread:{THREAD}",
                "workflow:run-1",
                {
                    "run_id": run_id,
                    "trace_id": trace_id,
                    "task": "triage",
                    "title": "Triage",
                    "actor": "a",
                    "mode": "replace",
                },
                THREAD,
            ),
        )
        await append(tx, NewEvent(run_progress, f"run:{run_id}", "workflow:run-1", {"summary": "working"}, THREAD))
        await append(
            tx,
            NewEvent(
                run_finished,
                f"run:{run_id}",
                "workflow:run-1",
                {"state": "done", "outcome": "asked", "summary": "asked.", "duration_ms": 5, "waiting": "reply"},
                THREAD,
            ),
        )
    await db.query("update threads set status_comment_id = 42 where thread = $1", (THREAD,))
    before = await db.query("select * from runs")

    assert await rebuild(db) == 3

    assert await db.query("select * from runs") == before
    assert before[0]["state"] == "done"
    assert before[0]["waiting"] == "reply"
    assert await db.query("select trace_id, run_id, status_comment_id from threads") == [
        {"trace_id": trace_id, "run_id": run_id, "status_comment_id": 42}
    ]


async def test_traced_queries_record_statements_but_never_parameters(db: Db, capfire: CaptureLogfire) -> None:
    logfire.instrument_asyncpg()
    await db.query("select $1::text as payload", ("event payload text",))
    statements = [dict(span.attributes or {}).get("db.statement") for span in capfire.exporter.exported_spans]
    assert "select $1::text as payload" in statements
    assert "event payload text" not in repr(capfire.exporter.exported_spans_as_dict())
