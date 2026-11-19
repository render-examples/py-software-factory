"""Thread keys: a channel's unit of conversation, `<channel>:<id>`. GitHub is the first channel."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, NewType

ThreadKey = NewType("ThreadKey", str)

_FORMAT = re.compile(r"^[a-z][a-z0-9-]{0,31}:\S{1,360}$")


def is_thread_key(value: object) -> bool:
    return isinstance(value, str) and _FORMAT.fullmatch(value) is not None


def thread_key(value: str) -> ThreadKey:
    if not is_thread_key(value):
        raise ValueError("expected a <channel>:<id> thread key")
    return ThreadKey(value)


@dataclass(frozen=True)
class GitHubThread:
    kind: Literal["issue", "pr"]
    number: int

    @property
    def name(self) -> str:
        """ "issue#12" or "pr#34" """
        return f"{self.kind}#{self.number}"


def github_thread(repo: str, thread: GitHubThread) -> ThreadKey:
    """`github:owner/name#issue-12` or `github:owner/name#pr-34`."""
    return thread_key(f"github:{repo}#{thread.kind}-{thread.number}")
