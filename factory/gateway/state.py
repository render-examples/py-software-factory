from __future__ import annotations

from factory.db import Queryable
from factory.gateway.routes import ThreadState, WaitingRun
from factory.threads import ThreadKey


async def thread_state(db: Queryable, thread: ThreadKey) -> ThreadState:
    rows = await db.query(
        "select r.task, r.run_id from threads t join runs r on r.run_id = t.trace_id "
        "where t.thread = $1 and r.waiting = 'reply'",
        (thread,),
    )
    return ThreadState(WaitingRun(rows[0]["task"], rows[0]["run_id"]) if rows else None)
