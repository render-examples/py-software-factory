"""The status comment: the App-authored comment on an issue or PR that names the run holding its claim."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol

from factory.events import FactoryEvent, emit, warn
from factory.github import IssueComment, RunKind, RunState, StatusMarker, find_marked_comment, render_marker

WORKING_LABEL = "factory:working"
"""On the issue or PR while a GitHub run is active."""

_ACTIVE_STATUSES = frozenset({"pending", "running", "paused"})


@dataclass(frozen=True)
class Subject:
    kind: Literal["issue", "pr"]
    number: int

    @property
    def name(self) -> str:
        """ "issue#12" or "pr#34" """
        return f"{self.kind}#{self.number}"


@dataclass(frozen=True)
class StatusLink:
    label: str
    url: str


class StatusGitHub(Protocol):
    """The GitHub calls the status comment needs."""

    async def bot_login(self) -> str: ...
    async def list_issue_comments(self, number: int) -> list[IssueComment]: ...
    async def create_comment(self, number: int, body: str) -> IssueComment: ...
    async def update_comment(self, comment_id: int, body: str) -> IssueComment: ...
    async def ensure_label(self, name: str, color: str, description: str = "") -> None: ...
    async def add_labels(self, number: int, labels: Sequence[str]) -> None: ...
    async def remove_label(self, number: int, label: str) -> None: ...


class RunNotFoundError(Exception):
    """The run no longer exists. Raised by RunControl.get_status for a confirmed 404."""


class RunControl(Protocol):
    """The Render calls that stop a previous run, injectable for tests. Reads and cancels task runs and flow
    runs alike."""

    async def get_status(self, run_id: str, kind: RunKind) -> str:
        """The run's status. Raises RunNotFoundError when the run doesn't exist."""
        ...

    async def cancel(self, run_id: str, kind: RunKind) -> None: ...

    async def terminate_sandbox(self, sandbox_id: str) -> None:
        """Idempotent: a sandbox that no longer exists counts as terminated."""
        ...


async def read_status(gh: StatusGitHub, number: int) -> tuple[IssueComment, StatusMarker] | None:
    """The bot's status comment on the issue or PR, and the run it records."""
    return find_marked_comment(await gh.list_issue_comments(number), StatusMarker, await gh.bot_login())


@dataclass(frozen=True)
class StopContext:
    """Who stops a run and why, for the `run.finished` event recorded on the stopped run's behalf."""

    repo: str
    subject: str
    by: str
    """The run doing the stopping."""
    outcome: Literal["superseded", "cancelled"]


async def stop_run(runs: RunControl, status: StatusMarker, context: StopContext) -> bool:
    """Stops the run a status marker records: cancels it if it's still active, then terminates the sandbox it
    recorded. Returns whether the run was still active.

    Render kills a cancelled run without running its `finally`, so the sandbox would otherwise live until its
    timeout, and the run never records how it ended; this records `run.finished` for it.
    """
    root_run_id = status.root_run_id
    kind: RunKind = status.root_kind or "task"
    active = await _is_run_active(runs, root_run_id, kind)
    if active:
        await _cancel_active_run(runs, root_run_id, kind)
        ids = {"repo": context.repo, "subject": context.subject}
        if status.state == "running":
            payload = {
                "stage": status.stage,
                "state": "cancelled",
                "outcome": context.outcome,
                "stopped_by": context.by,
            }
            emit(FactoryEvent(root_run_id, "run.finished", payload, run_id=status.run_id, **ids))
        if root_run_id != status.run_id:
            payload = {
                "stage": status.root_stage or "workflow",
                "state": "cancelled",
                "outcome": context.outcome,
                "stopped_by": context.by,
                "current_run_id": status.run_id,
            }
            emit(FactoryEvent(root_run_id, "run.finished", payload, run_id=root_run_id, **ids))
    if status.state == "running" and status.sandbox_id:
        try:
            await runs.terminate_sandbox(status.sandbox_id)
        except Exception as error:
            warn("could not terminate the stopped run's sandbox", sandbox_id=status.sandbox_id, error=str(error))
    return active


async def _is_run_active(runs: RunControl, run_id: str, kind: RunKind) -> bool:
    """Whether a run is pending, running, or paused. A confirmed 404 is finished; unknown state fails closed."""
    try:
        return await runs.get_status(run_id, kind) in _ACTIVE_STATUSES
    except RunNotFoundError:
        return False
    except Exception as error:
        warn("could not read run; refusing to assume it finished", run_id=run_id, kind=kind, error=str(error))
        raise


async def _cancel_active_run(runs: RunControl, run_id: str, kind: RunKind) -> None:
    """Cancels a run. A cancel that fails because the run just finished is fine; one that leaves it running
    raises."""
    try:
        await runs.cancel(run_id, kind)
    except Exception:
        if await _is_run_active(runs, run_id, kind):
            raise


@dataclass(frozen=True)
class RunIdentity:
    """Which Workflows run is acting. For a flow, every step acts as the flow run."""

    trace_id: str
    """The root run: the root task run, or the flow run."""
    run_id: str
    """This task run, or the flow run."""
    root_kind: RunKind = "task"


@dataclass(frozen=True)
class TakeOverResult:
    acquired: bool
    comment_id: int | None
    previous_run_id: str | None
    cancelled_previous: bool


async def take_over(
    *,
    gh: StatusGitHub,
    repo: str,
    subject: Subject,
    task: str,
    title: str,
    identity: RunIdentity,
    runs: RunControl,
    mode: Literal["replace", "continue"] = "replace",
) -> TakeOverResult:
    """Claims the issue or PR for this run: stops the run the status comment names, then rewrites the comment to
    name this run. The comment is the claim, so the caller must confirm it still names this run before relying on
    it. In "continue" mode a subtask may continue only the root run that already owns the subject."""
    current = await read_status(gh, subject.number)
    current_trace_id = current[1].root_run_id if current else None
    if mode == "continue" and current_trace_id != identity.trace_id:
        return TakeOverResult(
            acquired=False,
            comment_id=current[0].id if current else None,
            previous_run_id=current[1].run_id if current else None,
            cancelled_previous=False,
        )
    previous = current[1] if current and current[1].run_id != identity.run_id else None
    cancelled_previous = False
    if previous is not None and previous.root_run_id != identity.trace_id:
        stop = StopContext(repo=repo, subject=subject.name, by=identity.run_id, outcome="superseded")
        cancelled_previous = await stop_run(runs, previous, stop)
    if current is not None and current_trace_id == identity.trace_id:
        root_stage = current[1].root_stage or current[1].stage
    else:
        root_stage = task
    body = _status_body(
        StatusUpdate(
            number=subject.number,
            identity=identity,
            task=task,
            title=title,
            state="running",
            root_stage=root_stage,
        )
    )
    if current is not None:
        comment = await gh.update_comment(current[0].id, body)
    else:
        comment = await gh.create_comment(subject.number, body)
    await _add_working_label(gh, subject.number)
    return TakeOverResult(
        acquired=True,
        comment_id=comment.id,
        previous_run_id=previous.run_id if previous else None,
        cancelled_previous=cancelled_previous,
    )


@dataclass(frozen=True)
class StatusUpdate:
    number: int
    identity: RunIdentity
    task: str
    """The registered task or flow name."""
    title: str
    """The run's name in the comment, such as "PR Reviewer"."""
    state: RunState
    summary: str = ""
    links: Sequence[StatusLink] = field(default_factory=tuple[StatusLink, ...])
    sandbox_id: str | None = None
    """The run's open sandbox, recorded while it's running so a run that stops this one can terminate it."""
    root_stage: str | None = None
    """Registered name of the root task or flow."""


async def set_status(gh: StatusGitHub, update: StatusUpdate) -> bool:
    """Updates the status comment. Never overwrites a newer run's status, and a rejected trigger never overwrites
    a real run's record. Returns whether it wrote."""
    current = await read_status(gh, update.number)
    if current is None:
        await gh.create_comment(update.number, _status_body(update))
        return True
    comment, data = current
    ours = data.run_id == update.identity.run_id
    if not ours and (data.state == "running" or (update.state == "rejected" and data.state != "rejected")):
        return False
    root_stage = update.root_stage
    if root_stage is None and data.root_run_id == update.identity.trace_id:
        root_stage = data.root_stage
    await gh.update_comment(comment.id, _status_body(update, root_stage=root_stage))
    return True


async def finish_status(gh: StatusGitHub, update: StatusUpdate, *, root_finished: bool = False) -> bool:
    """Records a terminal state, and removes the working label when the root run finishes."""
    if update.state == "running":
        raise ValueError("finish_status takes a terminal state")
    wrote = await set_status(gh, update)
    is_root = update.identity.trace_id == update.identity.run_id
    if wrote and (is_root or root_finished):
        await _remove_working_label(gh, update.number)
    return wrote


def _status_body(s: StatusUpdate, *, root_stage: str | None = None) -> str:
    detail = f": {s.summary}" if s.summary else ""
    line = {
        "running": f"**{s.title}** is running (run `{s.identity.run_id}`){detail}",
        "done": f"**{s.title}** finished{detail}",
        "failed": f"**{s.title}** failed{detail}",
        "cancelled": f"**{s.title}** was cancelled{detail}",
        "rejected": f"**{s.title}** did not run{detail}",
    }[s.state]
    links = [f"- [{link.label}]({link.url})" for link in s.links]
    footer = [] if s.state == "running" else [f"<sub>run `{s.identity.run_id}`</sub>"]
    identity = s.identity
    marker = render_marker(
        StatusMarker(
            run_id=identity.run_id,
            trace_id=identity.trace_id,
            root_kind=identity.root_kind if identity.root_kind != "task" else None,
            root_stage=root_stage or s.root_stage,
            stage=s.task,
            title=s.title,
            state=s.state,
            at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            sandbox_id=s.sandbox_id if s.state == "running" else None,
        )
    )
    parts = [line]
    if links:
        parts += ["", *links]
    if footer:
        parts += ["", *footer]
    parts += ["", marker]
    return "\n".join(parts)


async def _add_working_label(gh: StatusGitHub, number: int) -> None:
    """Labels are cosmetic: failures are logged, never fail a run."""
    try:
        await gh.ensure_label(WORKING_LABEL, "fbca04", "software-factory is working on this")
        await gh.add_labels(number, [WORKING_LABEL])
    except Exception as error:
        warn("could not add working label", number=number, error=str(error))


async def _remove_working_label(gh: StatusGitHub, number: int) -> None:
    try:
        await gh.remove_label(number, WORKING_LABEL)
    except Exception as error:
        warn("could not remove working label", number=number, error=str(error))
