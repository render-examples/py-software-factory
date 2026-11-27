"""Schema migrations: numbered SQL files applied in order, once, under an advisory lock."""

from __future__ import annotations

import re
from importlib.resources import files

from factory.db.pool import Db, Queryable

_NAME = re.compile(r"^\d{3}_\w+\.sql$")


def migration_names() -> list[str]:
    return sorted(
        entry.name for entry in files("factory.db").joinpath("migrations").iterdir() if _NAME.match(entry.name)
    )


async def migrate(db: Db) -> list[str]:
    """Applies pending migrations and returns their names. Safe to run from several processes at once."""
    async with db.transaction() as tx:
        await tx.query("select pg_advisory_xact_lock(hashtext('software-factory:migrate'))")
        await tx.query(
            "create table if not exists schema_migrations "
            "(name text primary key, applied_at timestamptz not null default now())"
        )
        pending = await pending_migrations(tx)
        for name in pending:
            # Packaged migration files are trusted SQL, not input.
            await tx.script(files("factory.db").joinpath("migrations", name).read_text(encoding="utf-8"))
            await tx.query("insert into schema_migrations (name) values ($1)", (name,))
        return pending


async def pending_migrations(db: Queryable) -> list[str]:
    names = migration_names()
    rows = await db.query("select to_regclass('schema_migrations') is not null as exists")
    if not rows or not rows[0]["exists"]:
        return names
    applied = {row["name"] for row in await db.query("select name from schema_migrations")}
    return [name for name in names if name not in applied]
