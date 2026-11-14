"""Git Data API calls for one repository. Every call uses a contents:write token that stays on the host."""

from __future__ import annotations

import base64
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from factory.github._json import get
from factory.github.auth import github_token
from factory.github.client import RepoRef
from factory.github.request import GitHubHttp, Sleep, TokenProvider, encode_path
from factory.github.types import GitHubError

_SHA = re.compile(r"^[0-9a-f]{40}$")

FileMode = Literal["100644", "100755"]


@dataclass(frozen=True)
class TreeEntry:
    path: str
    mode: FileMode
    sha: str | None
    """None deletes the path from the base tree."""

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "mode": self.mode, "type": "blob", "sha": self.sha}


@dataclass(frozen=True)
class GitIdentity:
    name: str
    email: str
    date: str | None = None
    """ISO 8601. GitHub uses the current time when omitted."""

    def to_json(self) -> dict[str, str]:
        out = {"name": self.name, "email": self.email}
        if self.date is not None:
            out["date"] = self.date
        return out


@dataclass(frozen=True)
class GitCommit:
    sha: str
    tree_sha: str
    parents: tuple[str, ...]


class GitDataClient:
    def __init__(self, http: GitHubHttp, owner: str, repo: str) -> None:
        self._http = http
        self._prefix = f"/repos/{encode_path(owner)}/{encode_path(repo)}/git"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, body: Any = None) -> Any:
        # Blobs, trees, and commits are content-addressed and ref moves are compare-and-set, so every call is safe
        # to retry.
        return await self._http.json(method, f"{self._prefix}{path}", body=body, idempotent=True)

    async def get_commit(self, sha: str) -> GitCommit:
        result = await self._request("GET", f"/commits/{_require_sha(sha, 'commit')}")
        parents: list[Any] = get(result, "parents") or []
        commit = GitCommit(
            sha=_require_sha(get(result, "sha"), "commit"),
            tree_sha=_require_sha(get(get(result, "tree"), "sha"), "tree"),
            parents=tuple(_require_sha(get(p, "sha"), "parent") for p in parents),
        )
        if commit.sha != sha:
            raise RuntimeError(f"GitHub returned commit {commit.sha} for {sha}")
        return commit

    async def create_blob(self, content: bytes) -> str:
        """Returns the blob's SHA as GitHub computed it."""
        body = {"content": base64.b64encode(content).decode(), "encoding": "base64"}
        return _require_sha((await self._request("POST", "/blobs", body)).get("sha"), "blob")

    async def create_tree(self, base_tree: str, entries: Sequence[TreeEntry]) -> str:
        body = {"base_tree": _require_sha(base_tree, "base tree"), "tree": [e.to_json() for e in entries]}
        return _require_sha((await self._request("POST", "/trees", body)).get("sha"), "tree")

    async def create_commit(
        self, *, message: str, tree: str, parents: Sequence[str], author: GitIdentity, committer: GitIdentity
    ) -> str:
        body = {
            "message": message,
            "tree": _require_sha(tree, "tree"),
            "parents": [_require_sha(p, "parent") for p in parents],
            "author": author.to_json(),
            "committer": committer.to_json(),
        }
        return _require_sha((await self._request("POST", "/commits", body)).get("sha"), "commit")

    async def get_ref(self, branch: str) -> str | None:
        """The commit a branch points at, or None when the branch doesn't exist."""
        try:
            result = await self._request("GET", f"/ref/heads/{encode_path(branch)}")
        except GitHubError as error:
            if error.status == 404:
                return None
            raise
        if get(result, "ref") != f"refs/heads/{branch}":
            raise RuntimeError(f"GitHub returned {get(result, 'ref')} for refs/heads/{branch}")
        target = get(result, "object")
        if get(target, "type") != "commit":
            raise RuntimeError(f"refs/heads/{branch} points at a {get(target, 'type')}")
        return _require_sha(get(target, "sha"), "ref target")

    async def create_ref(self, branch: str, sha: str) -> None:
        """Raises GitHubError with status 422 when the branch already exists."""
        await self._request("POST", "/refs", {"ref": f"refs/heads/{branch}", "sha": _require_sha(sha, "commit")})

    async def update_ref(self, branch: str, sha: str, *, force: bool = False) -> None:
        """Without force, GitHub refuses (422) unless the move is a fast-forward."""
        body = {"sha": _require_sha(sha, "commit"), "force": force}
        await self._request("PATCH", f"/refs/heads/{encode_path(branch)}", body)


def git_data(
    repo: RepoRef,
    *,
    token: TokenProvider | None = None,
    api_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep | None = None,
) -> GitDataClient:
    async def contents_write_token() -> str:
        return await github_token(repo.installation_id, repo.name, {"contents": "write"}, transport=transport)

    http = GitHubHttp(
        token or contents_write_token,
        api_url=api_url,
        transport=transport,
        sleep=sleep,
        timeout_seconds=30,
        max_attempts=5,
    )
    return GitDataClient(http, repo.owner, repo.name)


def _require_sha(value: object, what: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise RuntimeError(f"Invalid {what} SHA: {value}")
    return value
