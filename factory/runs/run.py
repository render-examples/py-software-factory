"""Running GitHub work as a run on its issue's or PR's thread."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from factory.agents import AgentError, AgentErrorCode
from factory.config import RepoContext, get_config
from factory.db import Db, append, workflows_db
from factory.events import (
    EventIds,
    EventSpec,
    Payload,
    run_awaiting_reply,
    run_finished,
    run_progress,
    run_rejected,
    run_sandbox_attached,
    warn,
)
from factory.events.catalog import Link, RunMode
from factory.github import GitHubClient, github_for
from factory.runs.permission import check_permission
from factory.runs.run_control import RunControl
from factory.runs.status import StatusView, show_rejection, sync_status
from factory.runs.thread import StartingRun, is_current, retry_cleanup, run_event, start_on_thread, stop_trace
from factory.runs.trace import RunIdentity
from factory.runs.types import Cleanup, Displaced, ExpectedFailure, Outcome, StatusLink, TaskResult, TerminalState
from factory.threads import GitHubThread, ThreadKey, github_thread

D = TypeVar("D")

_AGENT_STOPS: dict[AgentErrorCode, str] = {
    "max_turns": "ran out of turns",
    "timeout": "timed out",
    "no_submission": "ended without submitting a result",
    "invalid_submission": "submitted an invalid result",
    "output_invalid": "submitted an invalid result",
}
"""Ways an agent stops that come from the model or its limits. Any other agent error is the platform's and fails
the run."""

_SUPERSEDED = ("superseded", "a newer run took over.")
_STALE_REPLY = ("stale_reply", "the issue moved on before this reply could resume it.")
FINAL_SYNC_ATTEMPTS = 3


class _Superseded(Exception):
    """Raised once a newer trace owns the thread."""


@dataclass(frozen=True)
class RunOptions:
    repo: RepoContext
    thread: GitHubThread
    task: str
    """The registered task name."""
    title: str
    """The run's name in the status comment, such as "PR Reviewer"."""
    actor: str
    runs: Callable[[], RunControl]
    """Built only when a displaced trace or unconfirmed cleanup needs stopping."""
    mode: RunMode = "replace"
    """Root runs replace another trace; children only continue their own; replies resume a waiting trace."""
    reply_to: str | None = None
    gh: GitHubClient | None = None
    db: Db | None = None


class ThreadRun:
    def __init__(self, identity: RunIdentity, opts: RunOptions, gh: GitHubClient, db: Db) -> None:
        self._identity = identity
        self._opts = opts
        self._gh = gh
        self._db = db
        self._key = github_thread(opts.repo.full_name, opts.thread)
        self._started = time.monotonic()
        self._previous: str | None = None
        self._shown: str | None = None
        self._displaced: Displaced | None = None
        self._cleanup = Cleanup()

    @property
    def gh(self) -> GitHubClient:
        return self._gh

    @property
    def repo(self) -> RepoContext:
        return self._opts.repo

    @property
    def thread(self) -> GitHubThread:
        return self._opts.thread

    @property
    def thread_key(self) -> ThreadKey:
        return self._key

    @property
    def trace_id(self) -> str:
        return self._identity.trace_id

    @property
    def run_id(self) -> str:
        return self._identity.run_id

    @property
    def event_ids(self) -> EventIds:
        return EventIds(self.trace_id, self.run_id, self.repo.full_name, self.thread.name)

    @property
    def displaced(self) -> Displaced | None:
        return self._displaced

    @property
    def cleanup(self) -> Cleanup:
        return self._cleanup

    async def assert_current(self) -> None:
        if not await is_current(self._db, self._key, self.trace_id, self.run_id):
            raise _Superseded

    async def report_progress(self, summary: str) -> None:
        await self.assert_current()
        await self._append(run_progress, run_progress.payload(summary=summary))
        await self._sync()

    async def await_reply(self) -> None:
        await self.assert_current()
        await self._append(run_awaiting_reply, run_awaiting_reply.payload())

    async def track_sandbox(self, sandbox_id: str) -> None:
        await self._append(run_sandbox_attached, run_sandbox_attached.payload(sandbox_id=sandbox_id))
        await self.assert_current()

    async def authorized(self) -> bool:
        return await check_permission(self._gh, self._opts.actor)

    async def reject(self) -> TaskResult[Any]:
        opts = self._opts
        summary = f"@{opts.actor} needs `{get_config().minimum_permission}` permission on this repository to start it."
        payload = run_rejected.payload(
            trace_id=self.trace_id, task=opts.task, title=opts.title, actor=opts.actor, reason=summary
        )
        await self._append(run_rejected, payload)
        view = StatusView(self.trace_id, self.run_id, opts.task, opts.title, "rejected", summary)
        try:
            await show_rejection(self._db, self._gh, self.repo.full_name, self.thread, view)
        except Exception as error:
            warn("could not show the rejection", run_id=self.run_id, error=str(error))
        return self._result("rejected", "permission_denied", summary)

    async def start(self) -> bool:
        opts = self._opts
        started = await start_on_thread(
            self._db,
            StartingRun(
                thread=self._key,
                run_id=self.run_id,
                trace_id=self.trace_id,
                task=opts.task,
                title=opts.title,
                actor=opts.actor,
                mode=opts.mode,
                reply_to=opts.reply_to,
            ),
        )
        self._previous = started.displaced if started else None
        return started is not None

    async def not_started(self) -> TaskResult[Any]:
        outcome, summary = _STALE_REPLY if self._opts.mode == "reply" else _SUPERSEDED
        return self._result("cancelled", outcome, summary)

    async def stop_displaced(self) -> None:
        retried = await retry_cleanup(self._db, self._opts.runs, self._key, self.run_id)
        unconfirmed = list(retried.unconfirmed)
        if self._previous is not None:
            summary = f"{self._opts.title} by @{self._opts.actor} took over."
            stopped = await stop_trace(self._db, self._opts.runs(), self._previous, by=self.run_id, summary=summary)
            if stopped.run_ids:
                self._displaced = Displaced(self._previous, stopped.run_ids)
            unconfirmed.extend(stopped.unconfirmed)
        self._cleanup = Cleanup(retried.confirmed, tuple(unconfirmed))
        await self._sync()

    async def finish(self, outcome: Outcome[D]) -> TaskResult[D]:
        await self.assert_current()
        return await self._end(
            outcome.state, outcome.outcome, outcome.summary, outcome.links, outcome.waiting, outcome.data
        )

    async def fail(self, error: Exception) -> TaskResult[Any]:
        """A superseded run ends quietly. An expected failure or an agent stop is shown and the run succeeds.
        Anything else is shown without its message, which can hold secrets or repository output, and re-raised."""
        if isinstance(error, _Superseded) or not await self._still_current():
            return await self._record("cancelled", *_SUPERSEDED)
        if isinstance(error, ExpectedFailure):
            return await self._end("failed", error.outcome, str(error))
        if isinstance(error, AgentError) and error.code in _AGENT_STOPS:
            warn("agent stopped", run_id=self.run_id, agent_id=error.agent_id, error=str(error)[:500])
            return await self._end("failed", "agent_stopped", f"the {error.agent_id} agent {_AGENT_STOPS[error.code]}.")
        warn("run failed", trace_id=self.trace_id, run_id=self.run_id, error=str(error)[:500])
        summary = f"something went wrong. Run `{self.run_id}` has the details in the Render Dashboard."
        try:
            await self._end("failed", "error", summary)
        except Exception as record_error:
            warn("could not record the failure", run_id=self.run_id, error=str(record_error))
        raise error

    async def _still_current(self) -> bool:
        try:
            return await is_current(self._db, self._key, self.trace_id, self.run_id)
        except Exception:
            return True

    async def _end(
        self,
        state: Literal["done", "failed"],
        outcome: str,
        summary: str,
        links: Sequence[StatusLink] = (),
        waiting: Literal["reply"] | None = None,
        data: D | None = None,
    ) -> TaskResult[D]:
        result = await self._record(state, outcome, summary, links, waiting, data)
        for attempt in range(1, FINAL_SYNC_ATTEMPTS + 1):
            if await self._sync() or attempt == FINAL_SYNC_ATTEMPTS:
                break
            await asyncio.sleep(0.5 * attempt)
        return result

    async def _record(
        self,
        state: Literal["done", "failed", "cancelled"],
        outcome: str,
        summary: str,
        links: Sequence[StatusLink] = (),
        waiting: Literal["reply"] | None = None,
        data: D | None = None,
    ) -> TaskResult[D]:
        payload = run_finished.payload(
            state=state,
            outcome=outcome,
            summary=summary,
            links=tuple(Link(label=link.label, url=link.url) for link in links),
            duration_ms=round((time.monotonic() - self._started) * 1000),
            waiting=waiting,
        )
        await self._append(run_finished, payload)
        return self._result(state, outcome, summary, data)

    def _result(self, state: TerminalState, outcome: str, summary: str, data: D | None = None) -> TaskResult[D]:
        return TaskResult(state, outcome, summary, self.trace_id, self.run_id, data)

    async def _append(self, spec: EventSpec[Any], payload: Payload) -> None:
        async with self._db.transaction() as tx:
            await append(tx, run_event(spec, self.run_id, self._key, payload))

    async def _sync(self) -> bool:
        try:
            self._shown = await sync_status(self._db, self._gh, self.repo.full_name, self.thread, self._shown)
        except Exception as error:
            warn("could not update the status comment", run_id=self.run_id, thread=self._key, error=str(error))
            return False
        return True


async def with_run(
    identity: RunIdentity, opts: RunOptions, work: Callable[[ThreadRun], Awaitable[Outcome[D]]]
) -> TaskResult[D]:
    """Runs `work` as a run on its thread.

    It checks the actor's current permission before anything else, so an unauthorized trigger displaces nothing;
    starts on the thread, stopping the trace it displaces; keeps the status view current; and records how the run
    ended.
    """
    gh = opts.gh or github_for(opts.repo)
    db = opts.db or workflows_db()
    run = ThreadRun(identity, opts, gh, db)
    try:
        if not await run.authorized():
            return await run.reject()
        if not await run.start():
            return await run.not_started()
        try:
            await run.stop_displaced()
            return await run.finish(await work(run))
        except Exception as error:
            return await run.fail(error)
    finally:
        if opts.gh is None:
            await gh.aclose()
