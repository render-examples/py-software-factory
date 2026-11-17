"""Workflow-owned GitHub operations for one repository. Agents never get this."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx

from factory.github._json import get
from factory.github.auth import app_jwt, github_token
from factory.github.request import GitHubHttp, Sleep, TokenProvider
from factory.github.types import (
    PERMISSIONS,
    Actor,
    GitHubError,
    InlineComment,
    Issue,
    IssueComment,
    LabelEvent,
    Permission,
    PullFile,
    PullRequest,
    PullRequestReview,
)


class RepoRef(Protocol):
    @property
    def owner(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def installation_id(self) -> int: ...


def github_for(
    repo: RepoRef,
    *,
    token: TokenProvider | None = None,
    api_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep | None = None,
) -> GitHubClient:
    """A client acting as the App installation that delivered the webhook. `token` overrides the installation
    token, for tests and scripts."""

    async def installation_token() -> str:
        return await github_token(repo.installation_id, repo.name, transport=transport)

    http = GitHubHttp(token or installation_token, api_url=api_url, transport=transport, sleep=sleep)
    return GitHubClient(repo.owner, repo.name, http)


_bot_logins: dict[str, str] = {}


class GitHubClient:
    def __init__(self, owner: str, repo: str, http: GitHubHttp) -> None:
        self.owner = owner
        self.repo = repo
        self.http = http
        self._base = f"/repos/{_segment(owner)}/{_segment(repo)}"

    async def aclose(self) -> None:
        await self.http.aclose()

    async def bot_login(self) -> str:
        """The App's bot login, `<app-slug>[bot]`, from GET /app with the App JWT. Cached per process."""
        key = self.http.api_url
        if key not in _bot_logins:

            async def jwt() -> str:
                return app_jwt()

            app_http = GitHubHttp(jwt, api_url=key, transport=self.http.transport)
            try:
                app = await app_http.json("GET", "/app")
            finally:
                await app_http.aclose()
            _bot_logins[key] = f"{app['slug']}[bot]"
        return _bot_logins[key]

    async def get_issue(self, number: int) -> Issue:
        return _issue(await self.http.json("GET", f"{self._base}/issues/{number}"))

    async def list_issue_comments(self, number: int) -> list[IssueComment]:
        """Every comment, oldest first."""
        return [_comment(c) for c in await self.http.paginate(f"{self._base}/issues/{number}/comments")]

    async def create_comment(self, number: int, body: str) -> IssueComment:
        return _comment(await self.http.json("POST", f"{self._base}/issues/{number}/comments", body={"body": body}))

    async def update_comment(self, comment_id: int, body: str) -> IssueComment:
        raw = await self.http.json(
            "PATCH", f"{self._base}/issues/comments/{comment_id}", body={"body": body}, idempotent=True
        )
        return _comment(raw)

    async def ensure_label(self, name: str, color: str, description: str = "") -> None:
        """Creates a repository label, confirming that a 422 really means it already exists."""
        try:
            await self.http.json(
                "POST", f"{self._base}/labels", body={"name": name, "color": color, "description": description}
            )
        except GitHubError as error:
            if error.status != 422 or not await self._label_exists(name):
                raise

    async def _label_exists(self, name: str) -> bool:
        try:
            await self.http.json("GET", f"{self._base}/labels/{_segment(name)}")
        except GitHubError as error:
            if error.status == 404:
                return False
            raise
        return True

    async def add_labels(self, number: int, labels: Sequence[str]) -> None:
        if not labels:
            return
        await self.http.json(
            "POST", f"{self._base}/issues/{number}/labels", body={"labels": list(labels)}, idempotent=True
        )

    async def remove_label(self, number: int, label: str) -> None:
        """A label that isn't on the issue (404) is fine."""
        try:
            await self.http.json("DELETE", f"{self._base}/issues/{number}/labels/{_segment(label)}")
        except GitHubError as error:
            if error.status != 404:
                raise

    async def get_permission(self, login: str) -> Permission:
        """The user's repository permission. `permission` folds maintain into write and triage into read, so this
        reads `role_name`, and for custom roles the permission flags. A non-collaborator (404) has "none"."""
        try:
            raw = await self.http.json("GET", f"{self._base}/collaborators/{_segment(login)}/permission")
        except GitHubError as error:
            if error.status == 404:
                return "none"
            raise
        return _permission(raw)

    async def get_label_events(self, number: int, label: str) -> list[LabelEvent]:
        """Times a label was applied to the issue or PR, oldest first."""
        events = await self.http.paginate(f"{self._base}/issues/{number}/events")
        return [
            LabelEvent(label, e["created_at"], _actor(e["actor"]) if e.get("actor") else None)
            for e in events
            if e.get("event") == "labeled" and get(e.get("label"), "name") == label
        ]

    async def get_pull(self, number: int) -> PullRequest:
        return _pull(await self.http.json("GET", f"{self._base}/pulls/{number}"))

    async def get_pull_diff(self, number: int) -> str:
        """The PR's unified diff."""
        return await self.http.text("GET", f"{self._base}/pulls/{number}", accept="application/vnd.github.diff")

    async def list_pull_files(self, number: int) -> list[PullFile]:
        files = await self.http.paginate(f"{self._base}/pulls/{number}/files")
        return [
            PullFile(
                filename=f["filename"],
                status=f["status"],
                additions=f["additions"],
                deletions=f["deletions"],
                changes=f["changes"],
                patch=f.get("patch"),
                previous_filename=f.get("previous_filename"),
            )
            for f in files
        ]

    async def list_reviews(self, pr: int) -> list[PullRequestReview]:
        return [_review(r) for r in await self.http.paginate(f"{self._base}/pulls/{pr}/reviews")]

    async def create_review(
        self,
        pr: int,
        *,
        commit_id: str,
        body: str,
        event: Literal["COMMENT"],
        comments: Sequence[InlineComment],
    ) -> PullRequestReview:
        """Posts a COMMENT review. The signature admits no other event: the factory never approves or requests
        changes."""
        if event != "COMMENT":
            raise ValueError("the factory posts COMMENT reviews only")
        payload = {
            "commit_id": commit_id,
            "body": body,
            "event": "COMMENT",
            "comments": [{"path": c.path, "line": c.line, "side": c.side, "body": c.body} for c in comments],
        }
        return _review(await self.http.json("POST", f"{self._base}/pulls/{pr}/reviews", body=payload))

    async def ensure_pull(self, *, head: str, base: str, title: str, body: str) -> tuple[PullRequest, bool]:
        """Makes a draft PR from `head` exist with this title and body.

        A replay updates the draft it opened. Once a person marks that PR ready, automation leaves it alone.
        Returns the PR and whether it was created.
        """
        existing = await self._find_open_pull(head)
        if existing is not None:
            _require_draft(existing)
            raw = await self.http.json(
                "PATCH", f"{self._base}/pulls/{existing.number}", body={"title": title, "body": body}
            )
            return _pull(raw), False
        try:
            raw = await self.http.json(
                "POST",
                f"{self._base}/pulls",
                body={"head": head, "base": base, "title": title, "body": body, "draft": True},
            )
            return _pull(raw), True
        except GitHubError as error:
            # Lost a race with another run, or a retried POST that had succeeded.
            if error.status == 422:
                raced = await self._find_open_pull(head)
                if raced is not None:
                    _require_draft(raced)
                    return raced, False
            raise

    async def _find_open_pull(self, head: str) -> PullRequest | None:
        open_pulls = await self.http.json(
            "GET", f"{self._base}/pulls", query={"head": f"{self.owner}:{head}", "state": "open", "per_page": 10}
        )
        return _pull(open_pulls[0]) if open_pulls else None


def reset_bot_login_cache() -> None:
    """Test seam."""
    _bot_logins.clear()


def _segment(value: str) -> str:
    return quote(value, safe="")


_GHOST = Actor("ghost", "User")


def _actor(user: Any) -> Actor:
    return Actor(user["login"], user["type"]) if user else _GHOST


def _issue(raw: Any) -> Issue:
    labels = tuple(
        name
        for name in (label if isinstance(label, str) else (label.get("name") or "") for label in raw["labels"])
        if name
    )
    return Issue(
        number=raw["number"],
        title=raw["title"],
        body=raw.get("body") or "",
        state=raw["state"],
        labels=labels,
        author=_actor(raw.get("user")),
        is_pull="pull_request" in raw,
        html_url=raw["html_url"],
    )


def _comment(raw: Any) -> IssueComment:
    return IssueComment(
        id=raw["id"],
        body=raw.get("body") or "",
        author=_actor(raw.get("user")),
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        html_url=raw["html_url"],
    )


_PERMISSION_FLAGS: tuple[tuple[str, Permission], ...] = (
    ("admin", "admin"),
    ("maintain", "maintain"),
    ("push", "write"),
    ("triage", "triage"),
    ("pull", "read"),
)


def _permission(raw: Any) -> Permission:
    role = get(raw, "role_name")
    for permission in PERMISSIONS:
        if role == permission:
            return permission
    flags = get(get(raw, "user"), "permissions")
    for flag, permission in _PERMISSION_FLAGS:
        if get(flags, flag):
            return permission
    for permission in PERMISSIONS:
        if get(raw, "permission") == permission:
            return permission
    return "none"


def _pull(raw: Any) -> PullRequest:
    return PullRequest(
        number=raw["number"],
        state=raw["state"],
        merged=bool(raw.get("merged", False)),
        draft=bool(raw.get("draft", False)),
        title=raw["title"],
        body=raw.get("body") or "",
        head_sha=raw["head"]["sha"],
        head_ref=raw["head"]["ref"],
        base_ref=raw["base"]["ref"],
        html_url=raw["html_url"],
    )


def _require_draft(pull: PullRequest) -> None:
    """Automation may update its draft, but never takes over a PR a person marked ready."""
    if not pull.draft:
        raise RuntimeError(f"Refusing to update PR #{pull.number}: it is no longer a draft")


def _review(raw: Any) -> PullRequestReview:
    return PullRequestReview(
        id=raw["id"],
        body=raw.get("body") or "",
        author=_actor(raw.get("user")),
        commit_id=raw["commit_id"],
        state=raw["state"],
        html_url=raw["html_url"],
    )
