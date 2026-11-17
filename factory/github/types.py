from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

Permission = Literal["none", "read", "triage", "write", "maintain", "admin"]
PERMISSIONS: Final[tuple[Permission, ...]] = ("none", "read", "triage", "write", "maintain", "admin")
"""Repository permission levels, lowest first. Custom roles map to the base level they extend."""


@dataclass(frozen=True)
class Actor:
    login: str
    type: str
    """ "User", "Bot", "Organization", ... """


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    state: str
    labels: tuple[str, ...]
    author: Actor
    is_pull: bool
    """True when the number is a pull request (the issues API serves both)."""
    html_url: str


@dataclass(frozen=True)
class IssueComment:
    id: int
    body: str
    author: Actor
    created_at: str
    updated_at: str
    html_url: str


@dataclass(frozen=True)
class LabelEvent:
    label: str
    created_at: str
    actor: Actor | None


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: str
    merged: bool
    title: str
    body: str
    head_sha: str
    head_ref: str
    base_ref: str
    html_url: str
    draft: bool = False


@dataclass(frozen=True)
class PullFile:
    filename: str
    status: str
    additions: int
    deletions: int
    changes: int
    patch: str | None = None
    """Absent for binary files and very large diffs."""
    previous_filename: str | None = None


@dataclass(frozen=True)
class InlineComment:
    """One inline comment, as GitHub's create-review API takes it."""

    path: str
    line: int
    body: str
    side: Literal["RIGHT"] = "RIGHT"


@dataclass(frozen=True)
class PullRequestReview:
    id: int
    body: str
    author: Actor
    commit_id: str
    state: str
    html_url: str


class GitHubError(Exception):
    def __init__(self, status: int, message: str, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body
