"""Real-Postgres fixtures. Tests that use them are skipped unless TEST_DATABASE_URL names a server they may create
databases on, such as `postgresql:///postgres`. They must run one at a time: an open transaction anywhere on the
server holds back every thread reader."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Generator, Iterator
from contextlib import contextmanager

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from factory.db import PROJECTIONS, Db, create_db, migrate

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
SKIP_REASON = "set TEST_DATABASE_URL (for example postgresql:///postgres) to run Postgres tests"


@contextmanager
def temporary_database() -> Generator[str]:
    """A new, empty database, dropped afterwards. Yields its connection string."""
    if not TEST_DATABASE_URL:
        pytest.skip(SKIP_REASON)
    name = f"sf_test_{uuid.uuid4().hex}"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(TEST_DATABASE_URL, dbname=name)
    finally:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))


async def _migrate(url: str) -> None:
    db = create_db(url, 1)
    try:
        await migrate(db)
    finally:
        await db.aclose()


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    """One migrated database per test module."""
    with temporary_database() as url:
        asyncio.run(_migrate(url))
        yield url


@pytest.fixture
async def db(database_url: str) -> AsyncIterator[Db]:
    """A pool on the module's database, emptied before each test."""
    pool = create_db(database_url, 5)
    tables = sql.SQL(", ").join(sql.Identifier(t) for t in ("events", *(p.table for p in PROJECTIONS)))
    await pool.query(sql.SQL("truncate {}").format(tables))
    try:
        yield pool
    finally:
        await pool.aclose()
