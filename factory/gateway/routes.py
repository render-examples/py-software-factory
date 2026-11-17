"""Maps one GitHub delivery to the run it starts, or why it starts nothing. No I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from factory.config import RunName, Tasks, get_config, resolve_repo
from factory.schemas import (
    AutopilotInput,
    CancelInput,
    PrReviewInput,
    RefreshSnapshotInput,
    SpecToPrInput,
    TaskInput,
    TriageInput,
)


@dataclass(frozen=True)
class Start:
    run: RunName
    input: dict[str, Any]
    """Already validated with the run's input model, as JSON."""
    idempotency_key: str
    subject: str | None = None
    """ "issue#12" or "pr#34", for logs."""


@dataclass(frozen=True)
class Ignore:
    reason: str


@dataclass(frozen=True)
class Invalid:
    """An authenticated delivery that maps to a run but not to a valid input for it: a bug worth a failed
    delivery."""

    run: RunName
    reason: str
    subject: str | None = None


Route = Start | Ignore | Invalid


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
    labels: list[_Label] = Field(default_factory=list[_Label])
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

    def start(
        run: RunName,
        model: type[TaskInput],
        fields: dict[str, Any],
        subject: str | None = None,
        idempotency_key: str = delivery_id,
    ) -> Route:
        try:
            parsed = model.model_validate({**base, **fields})
        except ValidationError as error:
            return Invalid(run, str(error), subject)
        return Start(run, parsed.model_dump(mode="json", exclude_none=True), idempotency_key, subject)

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
        subject = f"issue#{issue.number}"
        if label == config.labels.triage:
            return start(Tasks.TRIAGE, TriageInput, {"issue": issue.number, "actor": actor}, subject)
        if label == config.labels.spec_approved:
            # GitHub bumps updated_at when the label is applied.
            approval = {"kind": "label", "labeled_at": issue.updated_at}
            return start(
                Tasks.SPEC_TO_PR, SpecToPrInput, {"issue": issue.number, "actor": actor, "approval": approval}, subject
            )
        if label == config.labels.autopilot:
            return start(Tasks.AUTOPILOT, AutopilotInput, {"issue": issue.number, "actor": actor}, subject)
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
        return start(Tasks.PR_REVIEW, PrReviewInput, fields, f"pr#{pr.number}")

    if event == "issue_comment":
        if action != "created":
            return Ignore("unhandled action")
        comment_event: _IssueCommentEvent | None = parse(_IssueCommentEvent)
        if comment_event is None:
            return Ignore("malformed payload")
        issue = comment_event.issue
        comment = comment_event.comment
        is_pr = issue.is_pull_request
        subject = f"{'pr' if is_pr else 'issue'}#{issue.number}"
        first_line = re.split(r"\r?\n", comment.body or "", maxsplit=1)[0].strip()
        if first_line == f"{config.command_prefix} cancel":
            fields = {"number": issue.number, "kind": "pr" if is_pr else "issue", "actor": actor}
            return start(Tasks.CANCEL, CancelInput, fields, subject)
        if is_pr:
            return Ignore("comment on pull request")
        labels = {label.name for label in issue.labels}
        fields = {"issue": issue.number, "actor": actor, "comment_id": comment.id}
        if config.labels.autopilot in labels:
            return start(Tasks.AUTOPILOT, AutopilotInput, fields, subject)
        if config.labels.triage not in labels:
            return Ignore("issue not in triage")
        return start(Tasks.TRIAGE, TriageInput, fields, subject)

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
