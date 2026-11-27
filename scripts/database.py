from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, suppress
from typing import Protocol
from urllib.parse import parse_qs, unquote, urlsplit

from scripts.common import Check, redact

TIMEOUT_SECONDS = 15
DATABASE_DOC = "docs/configuration.md#database"
_DSN_PASSWORD_RE = re.compile(r"\bpassword\s*=\s*('(?:[^'\\]|\\.)*'|\S+)")


class DatabaseProbe(Protocol):
    async def ping(self) -> None: ...

    async def pending_migrations(self) -> list[str]: ...

    async def aclose(self) -> None: ...


OpenDb = Callable[[str], DatabaseProbe]


class _FactoryDbProbe:
    def __init__(self, url: str) -> None:
        from factory.db import create_db

        self._db = create_db(url, 1)

    async def ping(self) -> None:
        await self._db.query("select 1")

    async def pending_migrations(self) -> list[str]:
        from factory.db import pending_migrations

        return await pending_migrations(self._db)

    async def aclose(self) -> None:
        await self._db.aclose()


def url_secrets(url: str) -> list[str]:
    secrets = [url]
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    if parts is not None:
        userinfo, at, _ = parts.netloc.rpartition("@")
        if at:
            user, _, password = userinfo.partition(":")
            secrets += [userinfo, unquote(userinfo), password, unquote(password)]
            if len(user) >= 4:
                secrets += [user, unquote(user)]
        query = parse_qs(parts.query)
        secrets += query.get("password", [])
        secrets += [user for user in query.get("user", []) if len(user) >= 4]
    secrets += [match.group(1).strip("'") for match in _DSN_PASSWORD_RE.finditer(url)]
    return [secret for secret in secrets if secret]


def database_checks(environment: Mapping[str, str], *, open_db: OpenDb | None = None) -> list[Check]:
    url = environment.get("DATABASE_URL", "").strip()
    if not url:
        return [
            Check(
                "database",
                False,
                f"DATABASE_URL not set; connection and migration checks skipped (see {DATABASE_DOC})",
                required=False,
            )
        ]
    return check_database(url, open_db=open_db)


def check_database(url: str, *, open_db: OpenDb | None = None) -> list[Check]:
    with _quiet_driver_logs():
        return asyncio.run(check_database_async(url, open_db=open_db))


async def check_database_async(url: str, *, open_db: OpenDb | None = None) -> list[Check]:
    secrets = url_secrets(url)
    try:
        probe = (open_db or _FactoryDbProbe)(url)
    except Exception as error:
        return _unreachable(error, secrets)
    try:
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                await probe.ping()
        except Exception as error:
            return _unreachable(error, secrets)
        connected = Check("database connects", True, "select 1 succeeded")
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                pending = await probe.pending_migrations()
        except Exception as error:
            return [connected, Check("migrations current", False, _describe(error, secrets), required=False)]
        if pending:
            detail = f"{len(pending)} pending ({', '.join(pending)}); services apply them when they start"
            return [connected, Check("migrations current", False, detail, required=False)]
        return [connected, Check("migrations current", True, "no pending migrations")]
    finally:
        with suppress(Exception):
            async with asyncio.timeout(TIMEOUT_SECONDS):
                await probe.aclose()


def _unreachable(error: BaseException, secrets: list[str]) -> list[Check]:
    return [
        Check("database connects", False, _describe(error, secrets)),
        Check("migrations current", False, "skipped: database unreachable", required=False),
    ]


def _describe(error: BaseException, secrets: list[str]) -> str:
    if isinstance(error, TimeoutError) and not str(error):
        return f"timed out after {TIMEOUT_SECONDS}s"
    message = "; ".join(line.strip() for line in redact(str(error), secrets).splitlines() if line.strip())
    name = type(error).__name__
    return f"{name}: {message}" if message else name


@contextmanager
def _quiet_driver_logs() -> Generator[None]:
    logger = logging.getLogger("asyncpg")
    previous = logger.level
    logger.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        logger.setLevel(previous)
