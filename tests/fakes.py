from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast

from factory.github import Actor, GitHubClient, GitHubError, IssueComment, Permission
from factory.runs import RunNotFoundError

BOT = Actor("factory[bot]", "Bot")


@dataclass
class FakeGitHub:
    permissions: dict[str, Permission] = field(default_factory=dict[str, Permission])
    comments: dict[int, tuple[int, str]] = field(default_factory=dict[int, tuple[int, str]])
    labels: dict[int, set[str]] = field(default_factory=dict[int, set[str]])
    deleted: list[int] = field(default_factory=list[int])
    removed_labels: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])
    created: int = 0
    _next_id: int = 100

    def as_client(self) -> GitHubClient:
        return cast(GitHubClient, self)

    def bodies(self, number: int) -> list[str]:
        return [body for issue, body in self.comments.values() if issue == number]

    async def get_permission(self, login: str) -> Permission:
        return self.permissions.get(login, "write")

    async def bot_login(self) -> str:
        return BOT.login

    async def create_comment(self, number: int, body: str) -> IssueComment:
        await asyncio.sleep(0)
        self._next_id += 1
        self.created += 1
        self.comments[self._next_id] = (number, body)
        return self._comment(self._next_id, body)

    async def update_comment(self, comment_id: int, body: str) -> IssueComment:
        if comment_id not in self.comments:
            raise GitHubError(404, f"comment {comment_id} not found")
        self.comments[comment_id] = (self.comments[comment_id][0], body)
        return self._comment(comment_id, body)

    async def delete_comment(self, comment_id: int) -> None:
        self.deleted.append(comment_id)
        self.comments.pop(comment_id, None)

    async def ensure_label(self, name: str, color: str, description: str = "") -> None:
        return None

    async def add_labels(self, number: int, labels: Sequence[str]) -> None:
        self.labels.setdefault(number, set()).update(labels)

    async def remove_label(self, number: int, label: str) -> None:
        self.removed_labels.append((number, label))
        self.labels.setdefault(number, set()).discard(label)

    async def aclose(self) -> None:
        return None

    def _comment(self, comment_id: int, body: str) -> IssueComment:
        return IssueComment(comment_id, body, BOT, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", f"url/{comment_id}")


@dataclass
class FakeRuns:
    statuses: dict[str, str] = field(default_factory=dict[str, str])
    cancelled: list[str] = field(default_factory=list[str])
    terminated: list[str] = field(default_factory=list[str])
    fail_cancel: bool = False
    fail_terminate: bool = False

    async def get_status(self, run_id: str) -> str:
        status = self.statuses.get(run_id, "running")
        if status == "missing":
            raise RunNotFoundError(run_id)
        if status == "unreadable":
            raise RuntimeError("Render is unavailable")
        return status

    async def cancel(self, run_id: str) -> None:
        if self.fail_cancel:
            raise RuntimeError("cancel failed")
        self.cancelled.append(run_id)
        self.statuses[run_id] = "canceled"

    async def terminate_sandbox(self, sandbox_id: str) -> None:
        if self.fail_terminate:
            raise RuntimeError("terminate failed")
        self.terminated.append(sandbox_id)
