"""The status comment and `factory:working` label: a view of the thread's runs, synced from Postgres. Nothing reads
them back to decide anything."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from factory.db import Db, Row
from factory.events import warn
from factory.github import GitHubError, IssueComment, RunState, StatusMarker, render_marker
from factory.runs.types import StatusLink
from factory.threads import GitHubThread, ThreadKey, github_thread
from factory.tracing import trace_link

WORKING_LABEL = "factory:working"
"""On the issue or PR while any run of its current trace is running."""


class StatusGitHub(Protocol):
    async def create_comment(self, number: int, body: str) -> IssueComment: ...
    async def update_comment(self, comment_id: int, body: str) -> IssueComment: ...
    async def delete_comment(self, comment_id: int) -> None: ...
    async def ensure_label(self, name: str, color: str, description: str = "") -> None: ...
    async def add_labels(self, number: int, labels: Sequence[str]) -> None: ...
    async def remove_label(self, number: int, label: str) -> None: ...


@dataclass(frozen=True)
class StatusView:
    trace_id: str
    run_id: str
    task: str
    title: str
    state: RunState
    summary: str | None
    links: Sequence[StatusLink] = field(default_factory=tuple[StatusLink, ...])


async def sync_status(
    db: Db, gh: StatusGitHub, repo: str, thread: GitHubThread, shown: str | None = None
) -> str | None:
    """Shows the thread's current run: the run that started last while it runs, otherwise the trace's root.
    Returns the body shown, so the caller can skip identical rewrites."""
    key = github_thread(repo, thread)
    rows = await db.query(
        "select t.status_comment_id as comment_id, t.trace_id, "
        "exists (select 1 from runs where trace_id = t.trace_id and state = 'running') as working, "
        "r.run_id, r.task, r.title, r.state, r.summary, coalesce(r.links, '[]'::jsonb) as links "
        "from threads t join runs r on r.run_id in (t.run_id, t.trace_id) "
        "where t.thread = $1 "
        "order by (r.run_id = t.run_id and r.state = 'running') desc, (r.run_id = t.trace_id) desc "
        "limit 1",
        (key,),
    )
    if not rows:
        return None
    row = rows[0]
    body = status_body(_view(row), trace_link())
    if body == shown:
        return body
    comment_id = int(row["comment_id"]) if row["comment_id"] is not None else None
    written = await _write_comment(gh, thread.number, comment_id, body)
    if written != comment_id:
        await _save_comment(db, gh, key, comment_id, written, body)
    if row["working"]:
        await gh.ensure_label(WORKING_LABEL, "fbca04", "software-factory is working on this")
        await gh.add_labels(thread.number, [WORKING_LABEL])
    else:
        await gh.remove_label(thread.number, WORKING_LABEL)
    return body


async def show_rejection(db: Db, gh: StatusGitHub, repo: str, thread: GitHubThread, view: StatusView) -> None:
    """Comments on a rejected trigger only when the thread has no other runs, so strangers can't spam a busy one."""
    rows = await db.query(
        "select not exists (select 1 from runs where thread = $1 and run_id <> $2) as first",
        (github_thread(repo, thread), view.run_id),
    )
    if rows and rows[0]["first"]:
        await gh.create_comment(thread.number, status_body(view, trace_link()))


def status_body(view: StatusView, trace: str | None = None) -> str:
    """`trace` links the Logfire trace of the run writing the comment, which is the run the comment shows."""
    detail = f": {view.summary}" if view.summary else ""
    line = {
        "running": f"**{view.title}** is running (run `{view.run_id}`){detail}",
        "done": f"**{view.title}** finished{detail}",
        "failed": f"**{view.title}** failed{detail}",
        "cancelled": f"**{view.title}** was cancelled{detail}",
        "rejected": f"**{view.title}** did not run{detail}",
    }[view.state]
    parts = [line]
    if view.links:
        parts += ["", *(f"- [{link.label}]({link.url})" for link in view.links)]
    if trace:
        parts += ["", f"[Trace in Logfire]({trace})"]
    if view.state != "running":
        parts += ["", f"<sub>run `{view.run_id}`</sub>"]
    marker = StatusMarker(
        run_id=view.run_id, trace_id=view.trace_id, stage=view.task, title=view.title, state=view.state
    )
    parts += ["", render_marker(marker)]
    return "\n".join(parts)


def _view(row: Row) -> StatusView:
    links: list[Any] = row["links"] or []
    return StatusView(
        trace_id=row["trace_id"],
        run_id=row["run_id"],
        task=row["task"],
        title=row["title"],
        state=row["state"],
        summary=row["summary"],
        links=tuple(StatusLink(str(link["label"]), str(link["url"])) for link in links),
    )


async def _write_comment(gh: StatusGitHub, number: int, comment_id: int | None, body: str) -> int:
    if comment_id is not None:
        try:
            return (await gh.update_comment(comment_id, body)).id
        except GitHubError as error:
            if error.status != 404:
                raise
    return (await gh.create_comment(number, body)).id


async def _save_comment(
    db: Db, gh: StatusGitHub, thread: ThreadKey, replaced: int | None, created: int, body: str
) -> None:
    """Records the comment only if no concurrent sync recorded one first; the loser deletes its duplicate."""
    saved = await db.query(
        "update threads set status_comment_id = $1 where thread = $2 "
        "and status_comment_id is not distinct from $3::bigint returning thread",
        (created, thread, replaced),
    )
    if saved:
        return
    try:
        await gh.delete_comment(created)
    except Exception as error:
        warn("could not delete a duplicate status comment", thread=thread, comment_id=created, error=str(error))
    rows = await db.query("select status_comment_id from threads where thread = $1", (thread,))
    winner = rows[0]["status_comment_id"] if rows else None
    if winner is not None:
        await gh.update_comment(int(winner), body)
