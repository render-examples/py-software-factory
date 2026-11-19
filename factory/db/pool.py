"""Postgres connections: a lazily opened pool, plain queries, and transactions."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, LiteralString, Protocol

from psycopg import AsyncConnection, sql
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from factory.config import require_env

Query = LiteralString | sql.SQL | sql.Composed
Params = Sequence[object] | Mapping[str, object] | None
Row = dict[str, Any]

CONNECT_TIMEOUT_SECONDS = 10


class Queryable(Protocol):
    async def query(self, query: Query, params: Params = None) -> list[Row]: ...


class Tx(Queryable, Protocol):
    """A connection inside an open transaction."""


class Db(Queryable, Protocol):
    def transaction(self) -> AbstractAsyncContextManager[Tx]: ...
    async def aclose(self) -> None: ...


class _Connection:
    def __init__(self, connection: AsyncConnection[DictRow]) -> None:
        self._connection = connection

    async def query(self, query: Query, params: Params = None) -> list[Row]:
        async with self._connection.cursor() as cursor:
            await cursor.execute(query, params)
            return await cursor.fetchall() if cursor.description is not None else []


OnOpen = Callable[[Db], Awaitable[None]]


class PoolDb:
    """Opens on first use. A failed first connection raises the server's error, not a pool timeout."""

    def __init__(self, url: str, max_size: int, *, on_open: OnOpen | None = None) -> None:
        self._url = url
        self._on_open = on_open
        self._pool = AsyncConnectionPool(
            url,
            min_size=1,
            max_size=max_size,
            open=False,
            timeout=CONNECT_TIMEOUT_SECONDS,
            connection_class=AsyncConnection[DictRow],
            kwargs={"row_factory": dict_row, "autocommit": True},
        )
        self._lock = asyncio.Lock()
        self._ready = False
        self._opened = False

    async def query(self, query: Query, params: Params = None) -> list[Row]:
        await self._ensure_open()
        return await self._query(query, params)

    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        return self._transaction(ensure=True)

    async def aclose(self) -> None:
        if self._opened:
            await self._pool.close()

    async def _ensure_open(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            if not self._opened:
                probe = await AsyncConnection.connect(self._url, connect_timeout=CONNECT_TIMEOUT_SECONDS)
                await probe.close()
                await self._pool.open()
                self._opened = True
            if self._on_open is not None:
                await self._on_open(_Opened(self))
            self._ready = True

    async def _query(self, query: Query, params: Params) -> list[Row]:
        async with self._pool.connection() as connection:
            return await _Connection(connection).query(query, params)

    @asynccontextmanager
    async def _transaction(self, *, ensure: bool) -> AsyncGenerator[Tx]:
        if ensure:
            await self._ensure_open()
        async with self._pool.connection() as connection, connection.transaction():
            yield _Connection(connection)


class _Opened:
    """The pool as `on_open` sees it: already open, so it must not wait for itself to open."""

    def __init__(self, db: PoolDb) -> None:
        self._db = db

    async def query(self, query: Query, params: Params = None) -> list[Row]:
        return await self._db._query(query, params)

    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        return self._db._transaction(ensure=False)

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
