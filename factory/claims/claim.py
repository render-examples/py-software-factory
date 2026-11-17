"""Running GitHub work under a claim on its issue or PR."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar

from factory.agents import AgentError, AgentErrorCode
from factory.claims.permission import check_permission
from factory.claims.status import (
    RunControl,
    RunIdentity,
    StatusLink,
    StatusUpdate,
    Subject,
    finish_status,
    read_status,
    set_status,
    take_over,
)
from factory.config import RepoContext, get_config
from factory.events import FactoryEvent, emit
from factory.github import GitHubClient, RunState, github_for

D = TypeVar("D")

TerminalState = Literal["done", "failed", "cancelled", "rejected"]


@dataclass(frozen=True)
class Outcome(Generic[D]):
    """How the work ended. `summary` is shown on GitHub, so it must be safe to publish."""

    outcome: str
    summary: str
    state: Literal["done", "failed"] = "done"
    links: Sequence[StatusLink] = field(default_factory=tuple[StatusLink, ...])
    data: D | None = None
    """Typed data for the caller. It is returned to Workflows, never rendered on GitHub."""


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


class _Superseded(Exception):
    """Raised once the status comment names another run."""


_AGENT_STOPS: dict[AgentErrorCode, str] = {
    "max_turns": "ran out of turns",
    "timeout": "timed out",
    "no_submission": "ended without submitting a result",
    "invalid_submission": "submitted an invalid result",
    "output_invalid": "submitted an invalid result",
}
"""Ways an agent stops that come from the model or its limits. Any other agent error is the platform's and fails
the run."""


@dataclass(frozen=True)
class ClaimOptions:
    repo: RepoContext
    subject: Subject
    task: str
    """The registered task name; recorded in the status marker and in events."""
    title: str
    """The run's name in the status comment, such as "PR Reviewer"."""
    actor: str
    runs: RunControl
    mode: Literal["replace", "continue"] = "replace"
    """Root runs may replace another root; subtasks may only continue their own root."""
    gh: GitHubClient | None = None


class Claim:
    """What the work gets: this run's identity and its claim on the issue or PR."""

    def __init__(self, identity: RunIdentity, opts: ClaimOptions, gh: GitHubClient) -> None:
        self.gh = gh
        self.repo = opts.repo
        self.subject = opts.subject
        self.identity = identity
        self._opts = opts
        self._summary = ""
        self._sandbox_id: str | None = None
        self._started = time.monotonic()

    @property
    def trace_id(self) -> str:
        return self.identity.trace_id

    @property
    def run_id(self) -> str:
        return self.identity.run_id

    @property
    def event_ids(self) -> dict[str, str]:
        """The ids every event this run emits carries, including its agents' `agent.finished`."""
        return {
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "repo": self.repo.full_name,
            "subject": self.subject.name,
        }

    async def assert_current(self) -> None:
        """Ends the run as superseded once a newer run owns this issue or PR. Call before durable writes."""
        if not await self._holds_claim():
            raise _Superseded

    async def report_progress(self, summary: str) -> None:
        """Asserts the claim, then publishes `summary` in the status comment."""
        self._summary = summary
        await self._update_running()

    async def track_sandbox(self, sandbox_id: str) -> None:
        """Records the sandbox in the status comment, so a run that stops this one can terminate it. Call right
        after opening it."""
        self._sandbox_id = sandbox_id
        await self._update_running()

    async def _holds_claim(self) -> bool:
        current = await read_status(self.gh, self.subject.number)
        return current is not None and current[1].root_run_id == self.trace_id

    async def _update_running(self) -> None:
        await self.assert_current()
        # A newer run can take over between the check and the write; set_status refuses to overwrite its running
        # status.
        if not await set_status(self.gh, self._status("running", self._summary)):
            raise _Superseded

    def _status(self, state: RunState, summary: str, links: Sequence[StatusLink] = ()) -> StatusUpdate:
        return StatusUpdate(
            number=self.subject.number,
            identity=self.identity,
            task=self._opts.task,
            title=self._opts.title,
            state=state,
            summary=summary,
            links=links,
            sandbox_id=self._sandbox_id,
        )

    async def _authorized(self) -> bool:
        return await check_permission(self.gh, self._opts.actor)

    async def _reject(self) -> TaskResult[Any]:
        """A rejected trigger never held the claim; set_status keeps its record from replacing a real run's."""
        summary = (
            f"@{self._opts.actor} needs `{get_config().minimum_permission}` permission on this repository to start it."
        )
        await finish_status(self.gh, self._status("rejected", summary))
        return self._record("rejected", "permission_denied", summary)

    async def _acquire(self) -> None:
        """Stops the previous run and rewrites the status comment to name this one, then confirms no other run's
        write landed after it."""
        acquired = await take_over(
            gh=self.gh,
            repo=self.repo.full_name,
            subject=self.subject,
            task=self._opts.task,
            title=self._opts.title,
            identity=self.identity,
            runs=self._opts.runs,
            mode=self._opts.mode,
        )
        if not acquired.acquired:
            raise _Superseded
        await self.assert_current()

    async def _finish(self, outcome: Outcome[D]) -> TaskResult[D]:
        if not await self._publish(outcome.state, outcome.summary, outcome.links):
            raise _Superseded
        return self._record(outcome.state, outcome.outcome, outcome.summary, data=outcome.data)

    async def _fail(self, error: BaseException) -> TaskResult[Any]:
        """A superseded run and an expected failure end the run normally. Anything else is reported on GitHub
        without its message, which can hold secrets or repository output, and re-raised so the run fails."""
        if isinstance(error, _Superseded):
            return self._record("cancelled", "superseded", "a newer run took over.")
        if isinstance(error, ExpectedFailure):
            await self._publish("failed", str(error))
            return self._record("failed", error.outcome, str(error))
        if isinstance(error, AgentError) and error.code in _AGENT_STOPS:
            # The agent's own message can quote repository output, so it goes to the event, not GitHub.
            summary = f"the {error.agent_id} agent {_AGENT_STOPS[error.code]}."
            await self._publish("failed", summary)
            return self._record("failed", "agent_stopped", summary, error=str(error)[:500])
        summary = f"something went wrong. Run `{self.run_id}` has the details in the Render Dashboard."
        # The original error is what matters.
        with contextlib.suppress(Exception):
            await self._publish("failed", summary)
        self._record("failed", "error", summary, error=str(error)[:500])
        raise error

    async def _publish(self, state: TerminalState, summary: str, links: Sequence[StatusLink] = ()) -> bool:
        """Writes a final state while this run still holds the claim; False once another run's status is
        showing."""
        if not await self._holds_claim():
            return False
        return await finish_status(self.gh, self._status(state, summary, links))

    def _record(
        self, state: TerminalState, outcome: str, summary: str, *, data: D | None = None, **extra: Any
    ) -> TaskResult[D]:
        duration_ms = round((time.monotonic() - self._started) * 1000)
        payload = {"stage": self._opts.task, "state": state, "outcome": outcome, "duration_ms": duration_ms, **extra}
        emit(
            FactoryEvent(
                self.trace_id,
                "run.finished",
                payload,
                run_id=self.run_id,
                repo=self.repo.full_name,
                subject=self.subject.name,
            )
        )
        return TaskResult(state, outcome, summary, self.trace_id, self.run_id, data)


async def with_claim(
    identity: RunIdentity, opts: ClaimOptions, work: Callable[[Claim], Awaitable[Outcome[D]]]
) -> TaskResult[D]:
    """Runs `work` while this run claims the issue or PR.

    The status comment is the claim: the run it names holds it. with_claim checks the actor's current permission,
    stops the previous run and takes over the comment, keeps it current, and records how the run ended.
    """
    gh = opts.gh or github_for(opts.repo)
    claim = Claim(identity, opts, gh)
    try:
        if not await claim._authorized():
            return await claim._reject()
        await claim._acquire()
        return await claim._finish(await work(claim))
    except Exception as error:
        return await claim._fail(error)
    finally:
        if opts.gh is None:
            await gh.aclose()
