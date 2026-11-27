"""Real-Postgres fixtures. Tests that use them are skipped unless TEST_DATABASE_URL names a server they may create
databases on, such as `postgresql:///postgres`."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from factory.db import PROJECTIONS, Db, create_db, migrate

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
SKIP_REASON = "set TEST_DATABASE_URL (for example postgresql:///postgres) to run Postgres tests"


async def _execute(statement: str) -> None:
    connection = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        await connection.execute(statement)
    finally:
        await connection.close()


def _admin(statement: str) -> None:
    """On its own thread and loop, so sync fixtures and async tests can both create databases."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, _execute(statement)).result()


@contextmanager
def temporary_database() -> Generator[str]:
    """A new, empty database, dropped afterwards. Yields its connection string."""
    if not TEST_DATABASE_URL:
        pytest.skip(SKIP_REASON)
    name = f"sf_test_{uuid.uuid4().hex}"
    _admin(f'create database "{name}"')
    try:
        yield urlunsplit(urlsplit(TEST_DATABASE_URL)._replace(path=f"/{name}"))
    finally:
        _admin(f'drop database "{name}" with (force)')


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
    await pool.query(f"truncate events, {', '.join(projection.table for projection in PROJECTIONS)}")
    try:
        yield pool
    finally:
        await pool.aclose()
