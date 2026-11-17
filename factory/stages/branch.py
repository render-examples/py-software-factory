"""Trusted branch names and ownership checks for Spec-to-PR."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, cast
from urllib.parse import quote

from factory.changeset import Refusal
from factory.claims import ExpectedFailure
from factory.format import sanitize_inline
from factory.github import GitCommit, GitIdentity
from factory.schemas import Spec


class BranchGit(Protocol):
    async def get_ref(self, branch: str) -> str | None: ...
    async def get_commit(self, sha: str) -> GitCommit: ...


class IdentityGitHub(Protocol):
    async def bot_login(self) -> str: ...

    @property
    def http(self) -> IdentityHttp: ...


class IdentityHttp(Protocol):
    async def json(self, method: str, path: str) -> Any: ...


def branch_name(issue: int) -> str:
    return f"factory/issue-{issue}"


def commit_message(spec: Spec, issue: int) -> str:
    return f"{' '.join(spec.title.split())}\n\nRefs #{issue}"


def commit_date(now: datetime) -> str:
    return now.isoformat(timespec="seconds").replace("+00:00", "Z")


async def bot_identity(gh: IdentityGitHub, date: str) -> GitIdentity:
    login = await gh.bot_login()
    request = gh.http.json
    raw: Any = await request("GET", f"/users/{quote(login, safe='')}")
    user_id: object = cast(Any, raw).get("id") if isinstance(raw, dict) else None
    if not isinstance(user_id, int):
        raise RuntimeError(f"GET /users/{login} returned no id")
    return GitIdentity(login, f"{user_id}+{login}@users.noreply.github.com", date)


async def owned_commits(git: BranchGit, branch: str, base_sha: str, identity: GitIdentity) -> list[str]:
    tip = await git.get_ref(branch)
    if tip is None or tip == base_sha:
        return []
    commit = await git.get_commit(tip)
    committer = commit.committer
    ours = (
        committer is not None
        and committer.name == identity.name
        and committer.email.lower() == identity.email.lower()
        and len(commit.parents) == 1
    )
    if not ours:
        raise ExpectedFailure(
            f"the branch `{branch}` has commits the factory didn't make, so it was left alone. "
            "Delete or rename the branch and reapply the label to retry.",
            "branch_taken",
        )
    return [tip]


def refusal_summary(refused: list[Refusal]) -> str:
    shown = [sanitize_inline(item.detail) for item in refused[:10]]
    more = f"; and {len(refused) - len(shown)} more" if len(refused) > len(shown) else ""
    return f"publishing refused the change: {'; '.join(shown)}{more}."
