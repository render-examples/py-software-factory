"""Replay the event log into its projections. Run after a projection change that alters derived rows."""

from __future__ import annotations

import asyncio

from factory.config import require_env
from factory.db import create_db, rebuild


async def _rebuild() -> int:
    db = create_db(require_env("DATABASE_URL"), 1)
    try:
        return await rebuild(db)
    finally:
        await db.aclose()


def main() -> int:
    print(f"replayed {asyncio.run(_rebuild())} events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
