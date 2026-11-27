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
    outcome: str
    summary: str
    state: Literal["done", "failed"] = "done"
    links: Sequence[StatusLink] = field(default_factory=tuple[StatusLink, ...])
    data: D | None = None
    waiting: Literal["reply"] | None = None


@dataclass(frozen=True)
class TaskResult(Generic[D]):
    state: TerminalState
    outcome: str
    summary: str
    trace_id: str
    run_id: str
    data: D | None = None


class ExpectedFailure(Exception):
    def __init__(self, message: str, outcome: str = "failed") -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True)
class Displaced:
    trace_id: str
    run_ids: tuple[str, ...]


@dataclass(frozen=True)
class Cleanup:
    confirmed: tuple[str, ...] = ()
    unconfirmed: tuple[str, ...] = ()


class Run(Protocol):
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

    async def assert_current(self) -> None: ...

    async def report_progress(self, summary: str) -> None: ...

    async def await_reply(self) -> None: ...

    async def track_sandbox(self, sandbox_id: str) -> None: ...
