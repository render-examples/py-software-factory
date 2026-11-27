"""Maps one GitHub delivery to the run it starts, or why it starts nothing. No I/O: a decision that depends on the
thread's state comes back as a Lookup for the caller to resolve."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from factory.config import RunName, Tasks, get_config, resolve_repo
from factory.db import LogEvent, NewEvent
from factory.events import delivery_received
from factory.events.catalog import DeliveryReceived
from factory.schemas import (
    AutopilotInput,
    CancelInput,
    PrReviewInput,
    RefreshSnapshotInput,
    SpecToPrInput,
    TaskInput,
    TriageInput,
)
from factory.threads import GitHubThread, ThreadKey, github_thread


@dataclass(frozen=True)
class Start:
    run: RunName
    input: dict[str, Any]
    """Already validated with the run's input model, as JSON."""
    idempotency_key: str
    thread: ThreadKey | None = None


@dataclass(frozen=True)
class Ignore:
    reason: str


@dataclass(frozen=True)
class Invalid:
    """An authenticated delivery that maps to a run but not to a valid input for it: a bug worth a failed
    delivery."""

    run: RunName
    reason: str
    thread: ThreadKey | None = None


Decision = Start | Ignore | Invalid


@dataclass(frozen=True)
class WaitingRun:
    task: str
    run_id: str


@dataclass(frozen=True)
class ThreadState:
    waiting: WaitingRun | None
    """The thread's latest trace root, when it finished waiting for a reply."""


@dataclass(frozen=True)
class Lookup:
    thread: ThreadKey
    decide: Callable[[ThreadState], Decision]


Route = Decision | Lookup


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _Named(_Payload):
    full_name: str


class _Installation(_Payload):
    id: int


class _Sender(_Payload):
    login: str
    type: str


class _Envelope(_Payload):
    action: str | None = None
    repository: _Named | None = None
    installation: _Installation | None = None
    sender: _Sender | None = None


class _Label(_Payload):
    name: str


class _Issue(_Payload):
    number: int
    updated_at: str | None = None
    pull_request: Any = None

    @property
    def is_pull_request(self) -> bool:
        return self.pull_request is not None


class _IssuesEvent(_Payload):
    label: _Label | None = None
    issue: _Issue


class _Head(_Payload):
    sha: str


class _PullRequest(_Payload):
    number: int
    state: str
    head: _Head


class _PullRequestEvent(_Payload):
    label: _Label | None = None
    pull_request: _PullRequest


class _Comment(_Payload):
    id: int
    body: str | None = ""


class _IssueCommentEvent(_Payload):
    issue: _Issue
    comment: _Comment


class _PushEvent(_Payload):
    ref: str
    after: str
    deleted: bool = False


_SHA = re.compile(r"^[0-9a-f]{40}$")
_ZERO_SHA = "0" * 40


def route(event: str, payload: Any, delivery_id: str) -> Route:
    if event == "ping":
        return Ignore("ping")

    try:
        envelope = _Envelope.model_validate(payload)
    except ValidationError:
        return Ignore("malformed payload")
    if envelope.repository is None:
        return Ignore("no repository")
    if envelope.installation is None:
        return Ignore("no installation")
    repo = resolve_repo(envelope.repository.full_name, envelope.installation.id)
    if repo is None:
        return Ignore("unconfigured repo")
    if event != "push" and envelope.sender is not None and envelope.sender.type == "Bot":
        return Ignore("bot sender")

    config = get_config()
    action = envelope.action
    actor = envelope.sender.login if envelope.sender else ""
    base = {"repo": repo.full_name, "installation_id": repo.installation_id}

    def on(thread: GitHubThread) -> ThreadKey:
        return github_thread(repo.full_name, thread)

    def start(
        run: RunName,
        model: type[TaskInput],
        fields: dict[str, Any],
        thread: GitHubThread | None = None,
        idempotency_key: str = delivery_id,
    ) -> Decision:
        key = on(thread) if thread else None
        try:
            parsed = model.model_validate({**base, **fields})
        except ValidationError as error:
            return Invalid(run, str(error), key)
        return Start(run, parsed.model_dump(mode="json", exclude_none=True), idempotency_key, key)

    def parse(model: type[_Payload]) -> Any:
        try:
            return model.model_validate(payload)
        except ValidationError:
            return None

    if event == "issues":
        if action != "labeled":
            return Ignore("unhandled action")
        issues_event: _IssuesEvent | None = parse(_IssuesEvent)
        if issues_event is None:
            return Ignore("malformed payload")
        issue = issues_event.issue
        label = issues_event.label.name if issues_event.label else None
        if issue.is_pull_request:
            return Ignore("pull request")
        thread = GitHubThread("issue", issue.number)
        if label == config.labels.triage:
            return start(Tasks.TRIAGE, TriageInput, {"issue": issue.number, "actor": actor}, thread)
        if label == config.labels.spec_approved:
            # GitHub bumps updated_at when the label is applied.
            approval = {"kind": "label", "labeled_at": issue.updated_at}
            fields = {"issue": issue.number, "actor": actor, "approval": approval}
            return start(Tasks.SPEC_TO_PR, SpecToPrInput, fields, thread)
        if label == config.labels.autopilot:
            return start(Tasks.AUTOPILOT, AutopilotInput, {"issue": issue.number, "actor": actor}, thread)
        return Ignore("unhandled label")

    if event == "pull_request":
        if action != "labeled":
            return Ignore("unhandled action")
        pr_event: _PullRequestEvent | None = parse(_PullRequestEvent)
        if pr_event is None:
            return Ignore("malformed payload")
        pr = pr_event.pull_request
        if (pr_event.label.name if pr_event.label else None) != config.labels.pr_review:
            return Ignore("unhandled label")
        if pr.state != "open":
            return Ignore("pull request not open")
        fields = {"pr": pr.number, "head_sha": pr.head.sha, "actor": actor}
        return start(Tasks.PR_REVIEW, PrReviewInput, fields, GitHubThread("pr", pr.number))

    if event == "issue_comment":
        if action != "created":
            return Ignore("unhandled action")
        comment_event: _IssueCommentEvent | None = parse(_IssueCommentEvent)
        if comment_event is None:
            return Ignore("malformed payload")
        issue = comment_event.issue
        comment = comment_event.comment
        is_pr = issue.is_pull_request
        first_line = re.split(r"\r?\n", comment.body or "", maxsplit=1)[0].strip()
        if first_line == f"{config.command_prefix} cancel":
            kind = "pr" if is_pr else "issue"
            fields = {"number": issue.number, "kind": kind, "actor": actor}
            return start(Tasks.CANCEL, CancelInput, fields, GitHubThread(kind, issue.number))
        if is_pr:
            return Ignore("comment on pull request")
        thread = GitHubThread("issue", issue.number)

        def resume(state: ThreadState) -> Decision:
            waiting = state.waiting
            if waiting is None:
                return Ignore("thread is not waiting for a reply")
            fields = {"issue": issue.number, "actor": actor, "comment_id": comment.id, "reply_to": waiting.run_id}
            if waiting.task == Tasks.TRIAGE:
                return start(Tasks.TRIAGE, TriageInput, fields, thread)
            if waiting.task == Tasks.AUTOPILOT:
                return start(Tasks.AUTOPILOT, AutopilotInput, fields, thread)
            return Ignore("thread is not waiting for a reply")

        return Lookup(on(thread), resume)

    if event == "push":
        push: _PushEvent | None = parse(_PushEvent)
        if push is None:
            return Ignore("malformed payload")
        if push.ref != f"refs/heads/{repo.config.base_branch}":
            return Ignore("not base branch")
        if push.deleted:
            return Ignore("branch deleted")
        if not _SHA.fullmatch(push.after) or push.after == _ZERO_SHA:
            return Ignore("invalid sha")
        return start(
            Tasks.REFRESH_SNAPSHOT,
            RefreshSnapshotInput,
            {"sha": push.after, "actor": actor},
            idempotency_key=f"{repo.full_name}@{push.after}",
        )

    return Ignore("unhandled event")


def delivery_stream(delivery_id: str) -> str:
    return f"delivery:github:{delivery_id}"


def delivery_event(delivery_id: str, event: str, decision: Decision) -> NewEvent[DeliveryReceived]:
    """The decision as recorded. Its stream name is also its idempotency key, so a delivery is recorded once."""
    if isinstance(decision, Ignore):
        payload = DeliveryReceived(event=event, ignored=decision.reason[:200])
        thread = None
    elif isinstance(decision, Invalid):
        payload = DeliveryReceived(event=event, task=decision.run, ignored="invalid task input")
        thread = decision.thread
    else:
        payload = DeliveryReceived(
            event=event,
            repo=str(decision.input["repo"]),
            task=decision.run,
            input=decision.input,
            idempotency_key=decision.idempotency_key,
        )
        thread = decision.thread
    stream = delivery_stream(delivery_id)
    return NewEvent(delivery_received, stream, "github", payload, thread, stream)


def recorded_decision(event: LogEvent) -> Decision:
    recorded = delivery_received.parse(event.payload)
    task = cast(RunName, recorded.task) if recorded.task else None
    if task and recorded.input is not None and recorded.idempotency_key:
        return Start(task, recorded.input, recorded.idempotency_key, event.thread)
    if task:
        return Invalid(task, recorded.ignored or "invalid task input", event.thread)
    return Ignore(recorded.ignored or "ignored")
