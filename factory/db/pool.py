"""Postgres on asyncpg: a lazily opened pool, plain queries, transactions, and migration scripts.

The same pool backs Pydantic AI Harness's spend store, which takes an asyncpg pool.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import weakref
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, LiteralString, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

from factory.config import require_env

Query = LiteralString
Params = Sequence[object]
Row = dict[str, Any]

CONNECT_TIMEOUT_SECONDS = 10


class Queryable(Protocol):
    async def query(self, query: Query, params: Params = ()) -> list[Row]: ...


class Tx(Queryable, Protocol):
    """A connection inside an open transaction."""

    async def script(self, statements: str) -> None:
        """Runs trusted, parameterless SQL that may hold several statements, such as a migration file."""
        ...


class Db(Queryable, Protocol):
    def transaction(self) -> AbstractAsyncContextManager[Tx]: ...
    async def pool(self) -> asyncpg.Pool:
        """The open pool, for libraries that take an asyncpg pool."""
        ...

    async def aclose(self) -> None: ...


class _Connection:
    def __init__(self, connection: asyncpg.pool.PoolConnectionProxy) -> None:
        self._connection = connection

    async def query(self, query: Query, params: Params = ()) -> list[Row]:
        return [dict(row) for row in await self._connection.fetch(query, *params)]

    async def script(self, statements: str) -> None:
        await self._connection.execute(statements)


async def _json_codecs(connection: asyncpg.Connection) -> None:
    for name in ("json", "jsonb"):
        await connection.set_type_codec(name, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


def connect_options(url: str) -> tuple[str, ssl.SSLContext | None]:
    """libpq's `sslrootcert=system` (verify against the system trust store) as an asyncpg TLS context."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if ("sslrootcert", "system") not in query:
        return url, None
    kept = [(key, value) for key, value in query if key not in {"sslrootcert", "sslmode"}]
    return urlunsplit(parts._replace(query=urlencode(kept))), ssl.create_default_context()


OnOpen = Callable[[Db], Awaitable[None]]


class PoolDb:
    """Opens on first use. A failed first connection raises the server's error, not a pool timeout."""

    def __init__(self, url: str, max_size: int, *, on_open: OnOpen | None = None) -> None:
        self._url = url
        self._max_size = max_size
        self._on_open = on_open
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()
        self._ready = False

    async def query(self, query: Query, params: Params = ()) -> list[Row]:
        return await self._query(await self.pool(), query, params)

    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        return self._transaction(ensure=True)

    async def pool(self) -> asyncpg.Pool:
        await self._ensure_open()
        assert self._pool is not None
        return self._pool

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def _ensure_open(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            if self._pool is None:
                dsn, context = connect_options(self._url)
                self._pool = await asyncpg.create_pool(
                    dsn,
                    min_size=1,
                    max_size=self._max_size,
                    init=_json_codecs,
                    timeout=CONNECT_TIMEOUT_SECONDS,
                    ssl=context,
                )
            if self._on_open is not None:
                await self._on_open(_Opened(self))
            self._ready = True

    @staticmethod
    async def _query(pool: asyncpg.Pool, query: Query, params: Params) -> list[Row]:
        async with pool.acquire() as connection:
            return await _Connection(connection).query(query, params)

    @asynccontextmanager
    async def _transaction(self, *, ensure: bool) -> AsyncGenerator[Tx]:
        if ensure:
            await self._ensure_open()
        assert self._pool is not None
        async with self._pool.acquire() as connection, connection.transaction():
            yield _Connection(connection)


class _Opened:
    """The pool as `on_open` sees it: already open, so it must not wait for itself to open."""

    def __init__(self, db: PoolDb) -> None:
        self._db = db

    async def query(self, query: Query, params: Params = ()) -> list[Row]:
        assert self._db._pool is not None
        return await self._db._query(self._db._pool, query, params)

    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        return self._db._transaction(ensure=False)

    async def pool(self) -> asyncpg.Pool:
        assert self._db._pool is not None
        return self._db._pool

    async def aclose(self) -> None:
        await self._db.aclose()


def create_db(url: str, max_size: int, *, on_open: OnOpen | None = None) -> Db:
    return PoolDb(url, max_size, on_open=on_open)


_workflows: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Db] = weakref.WeakKeyDictionary()

WORKFLOWS_POOL_SIZE = 3


def workflows_db() -> Db:
    """The Workflows service's pool for the running event loop. It applies pending migrations when it opens."""
    from factory.db.migrate import migrate

    loop = asyncio.get_running_loop()
    db = _workflows.get(loop)
    if db is None:

        async def apply(opened: Db) -> None:
            await migrate(opened)

        db = create_db(require_env("DATABASE_URL"), WORKFLOWS_POOL_SIZE, on_open=apply)
        _workflows[loop] = db
    return db
