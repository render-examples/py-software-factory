"""What a GitHub run's work sees, and what it returns."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Generic, Literal, Protocol, TypeVar

from factory.config import RepoContext
from factory.events import EventIds
from factory.github import GitHubClient
from factory.threads import GitHubThread, ThreadKey

D = TypeVar("D")

TerminalState = Literal["done", "failed", "cancelled", "rejected"]


@dataclass(frozen=True)
class StatusLink:
    label: str
    url: str


@dataclass(frozen=True)
class Outcome(Generic[D]):
    """How the work ended. `summary` is shown on GitHub, so it must be safe to publish."""

    outcome: str
    summary: str
    state: Literal["done", "failed"] = "done"
    links: Sequence[StatusLink] = field(default_factory=tuple[StatusLink, ...])
    data: D | None = None
    """Typed data for the caller. It is returned to Workflows, never rendered on GitHub."""
    waiting: Literal["reply"] | None = None
    """Set when the run asked the people on the thread something; a reply resumes its trace."""


@dataclass(frozen=True)
class TaskResult(Generic[D]):
    """What a GitHub run returns to Render Workflows."""

    state: TerminalState
    outcome: str
    summary: str
    trace_id: str
    run_id: str
    data: D | None = None


class ExpectedFailure(Exception):
    """An expected failure: its message is shown on GitHub and the run succeeds. Anything else fails the run.

    Use it only for messages safe to publish. Keep outcome names in snake_case.
    """

    def __init__(self, message: str, outcome: str = "failed") -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True)
class Displaced:
    """The trace this run took the thread from, and its runs that were still running."""

    trace_id: str
    run_ids: tuple[str, ...]


@dataclass(frozen=True)
class Cleanup:
    """Task runs and sandboxes of earlier runs that Render confirmed stopped, or still hasn't."""

    confirmed: tuple[str, ...] = ()
    unconfirmed: tuple[str, ...] = ()


class Run(Protocol):
    """This run's identity and its hold on its issue or PR."""

    @property
    def gh(self) -> GitHubClient: ...
    @property
    def repo(self) -> RepoContext: ...
    @property
    def thread(self) -> GitHubThread: ...
    @property
    def thread_key(self) -> ThreadKey: ...
    @property
    def trace_id(self) -> str: ...
    @property
    def run_id(self) -> str: ...
    @property
    def event_ids(self) -> EventIds: ...
    @property
    def displaced(self) -> Displaced | None: ...
    @property
    def cleanup(self) -> Cleanup: ...

    async def assert_current(self) -> None:
        """Ends the run as superseded once a newer trace owns the thread. Call before durable writes."""
        ...

    async def report_progress(self, summary: str) -> None:
        """Asserts ownership, records `summary`, and shows it in the status comment."""
        ...

    async def await_reply(self) -> None:
        """Marks this trace as waiting for a reply. Call just before posting what the reply answers."""
        ...

    async def track_sandbox(self, sandbox_id: str) -> None:
        """Records the sandbox, so a run that stops this one can terminate it. Call right after opening it."""
        ...
