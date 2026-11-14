from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from factory.github import GitHubError, GitHubHttp, InlineComment, github_for
from factory.github.client import GitHubClient

API = "https://api.github.test"

Reply = httpx.Response | Callable[[httpx.Request], httpx.Response] | Exception


class FakeGitHub:
    """Replies in order; the last reply repeats."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(request)
        return reply

    def body(self, index: int) -> Any:
        return json.loads(self.requests[index].content)


def http(fake: FakeGitHub, sleeps: list[float] | None = None) -> GitHubHttp:
    async def token() -> str:
        return "ghs_secret_token"

    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return GitHubHttp(token, api_url=API, transport=fake.transport, sleep=sleep)


def client(fake: FakeGitHub) -> GitHubClient:
    class Repo:
        owner = "acme"
        name = "web"
        installation_id = 7

    async def token() -> str:
        return "ghs_t"

    async def sleep(_: float) -> None:
        return None

    return github_for(Repo(), token=token, api_url=API, transport=fake.transport, sleep=sleep)


class TestGitHubHttp:
    async def test_sends_github_headers_and_the_token(self) -> None:
        fake = FakeGitHub(httpx.Response(200, json={"ok": True}))
        await http(fake).json("GET", "/repos/acme/web")
        request = fake.requests[0]
        assert str(request.url) == f"{API}/repos/acme/web"
        assert request.headers["authorization"] == "Bearer ghs_secret_token"
        assert request.headers["accept"] == "application/vnd.github+json"
        assert request.headers["user-agent"] == "software-factory"
        assert request.headers["x-github-api-version"] == "2022-11-28"

    async def test_scrubs_the_token_from_error_messages(self) -> None:
        fake = FakeGitHub(httpx.Response(401, text="bad token ghs_secret_token"))
        with pytest.raises(GitHubError) as caught:
            await http(fake).json("GET", "/x")
        assert caught.value.status == 401
        assert "ghs_secret_token" not in str(caught.value)
        assert "ghs_secret_token" not in caught.value.body

    async def test_retries_5xx_and_rate_limits_for_safe_methods_only(self) -> None:
        sleeps: list[float] = []
        fake = FakeGitHub(
            httpx.Response(502),
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(200, json={}),
        )
        await http(fake, sleeps).json("GET", "/x")
        assert len(fake.requests) == 3
        assert sleeps[1] == 2

        post = FakeGitHub(httpx.Response(502), httpx.Response(201, json={}))
        with pytest.raises(GitHubError):
            await http(post).json("POST", "/x", body={})
        assert len(post.requests) == 1

    async def test_fails_instead_of_sleeping_past_the_wait_cap(self) -> None:
        sleeps: list[float] = []
        fake = FakeGitHub(httpx.Response(429, headers={"retry-after": "3600"}), httpx.Response(200, json={}))
        with pytest.raises(GitHubError):
            await http(fake, sleeps).json("GET", "/x")
        assert sleeps == []

    async def test_never_sends_the_token_to_another_host(self) -> None:
        fake = FakeGitHub(httpx.Response(200, json=[], headers={"link": '<https://evil.test/next>; rel="next"'}))
        with pytest.raises(ValueError, match=r"Refusing to send a GitHub token to evil\.test"):
            await http(fake).paginate("/repos/acme/web/issues")
        assert len(fake.requests) == 1

    async def test_follows_next_links_on_the_same_api(self) -> None:
        fake = FakeGitHub(
            httpx.Response(200, json=[1, 2], headers={"link": f'<{API}/repos/acme/web/issues?page=2>; rel="next"'}),
            httpx.Response(200, json=[3]),
        )
        assert await http(fake).paginate("/repos/acme/web/issues") == [1, 2, 3]
        assert fake.requests[0].url.params["per_page"] == "100"


RAW_PULL = {
    "number": 9,
    "state": "open",
    "draft": True,
    "title": "t",
    "body": None,
    "head": {"sha": "b" * 40, "ref": "factory/issue-3", "repo": {"full_name": "acme/web"}},
    "base": {"sha": "c" * 40, "ref": "main"},
    "html_url": "https://github.test/pull/9",
}


class TestGitHubClient:
    async def test_ensure_pull_opens_a_draft_and_recovers_from_losing_a_race(self) -> None:
        fresh = FakeGitHub(httpx.Response(200, json=[]), httpx.Response(201, json=RAW_PULL))
        _, created = await client(fresh).ensure_pull(head="h", base="main", title="t", body="b")
        assert created
        assert fresh.requests[1].method == "POST"
        assert fresh.body(1) | {"head": "h", "base": "main", "draft": True} == fresh.body(1)

        raced = FakeGitHub(
            httpx.Response(200, json=[]),
            httpx.Response(422, text="A pull request already exists"),
            httpx.Response(200, json=[RAW_PULL]),
        )
        pull, created = await client(raced).ensure_pull(head="h", base="main", title="t", body="b")
        assert not created
        assert pull.number == 9

    async def test_posts_only_comment_reviews(self) -> None:
        review = {"id": 1, "body": "b", "user": None, "commit_id": "c" * 40, "state": "COMMENTED", "html_url": ""}
        fake = FakeGitHub(httpx.Response(200, json=review))
        gh = client(fake)
        comments = [InlineComment(path="a.py", line=3, body="x")]
        await gh.create_review(9, commit_id="c" * 40, body="b", event="COMMENT", comments=comments)
        assert fake.body(0)["event"] == "COMMENT"
        with pytest.raises(ValueError, match="COMMENT"):
            await gh.create_review(9, commit_id="c" * 40, body="b", event="APPROVE", comments=[])  # pyright: ignore[reportArgumentType]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ({"permission": "write", "role_name": "maintain"}, "maintain"),
            ({"permission": "read", "role_name": "triage"}, "triage"),
            ({"permission": "write", "role_name": "custom", "user": {"permissions": {"push": True}}}, "write"),
            ({"permission": "admin"}, "admin"),
            ({"permission": "bogus"}, "none"),
        ],
    )
    async def test_reads_the_real_permission_level(self, raw: dict[str, Any], expected: str) -> None:
        assert await client(FakeGitHub(httpx.Response(200, json=raw))).get_permission("u") == expected

    async def test_a_non_collaborator_has_no_permission(self) -> None:
        assert await client(FakeGitHub(httpx.Response(404))).get_permission("stranger") == "none"
