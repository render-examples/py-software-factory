"""HTTP for the GitHub REST API: auth headers, JSON, timeouts, and retries on 5xx and rate limits."""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from factory.github.auth import github_api_url
from factory.github.types import GitHubError

TokenProvider = Callable[[], Awaitable[str]]
"""Resolves the current token. Installation tokens expire, so it's called per request (github_token caches)."""

Sleep = Callable[[float], Awaitable[None]]
Query = Mapping[str, str | int | bool | None]

_DEFAULT_ACCEPT = "application/vnd.github+json"
_MAX_WAIT_SECONDS = 60.0
"""A rate limit that asks for a longer wait fails instead: sleeping in a task is wasted compute."""
_SAFE_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE"})
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


class GitHubHttp:
    def __init__(
        self,
        token: TokenProvider,
        *,
        api_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 15,
        max_attempts: int = 4,
        sleep: Sleep | None = None,
    ) -> None:
        """`max_attempts` counts the first. `transport` and `sleep` are test seams."""
        self.api_url = (api_url or github_api_url()).rstrip("/")
        self.transport = transport
        self._token = token
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._sleep = sleep or asyncio.sleep
        self._client: httpx.AsyncClient | None = None
        self._last_token = ""
        """Kept only to scrub it from error messages."""

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def json(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        accept: str | None = None,
        query: Query | None = None,
        idempotent: bool = False,
    ) -> Any:
        """JSON request; None for 204. POST and PATCH are retried only when `idempotent`: the caller knows
        whether repeating the write is harmless."""
        response = await self.send(method, path, body=body, accept=accept, query=query, idempotent=idempotent)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def text(self, method: str, path: str, *, accept: str | None = None, query: Query | None = None) -> str:
        """Raw text response, for media types such as application/vnd.github.diff."""
        return (await self.send(method, path, accept=accept, query=query)).text

    async def paginate(self, path: str, *, query: Query | None = None, max_pages: int = 30) -> list[Any]:
        """Every page of a list endpoint, in API order, following Link rel="next"."""
        items: list[Any] = []
        next_url: str | None = path
        page_query: Query | None = {"per_page": 100, **(query or {})}
        page = 0
        while next_url:
            if page >= max_pages:
                raise RuntimeError(f"GitHub GET {path}: more than {page} pages")
            response = await self.send("GET", next_url, query=page_query)
            items.extend(response.json())
            next_url = _next_link(response.headers.get("link"))
            # The next link carries the query.
            page_query = None
            page += 1
        return items

    async def send(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        accept: str | None = None,
        query: Query | None = None,
        idempotent: bool = False,
    ) -> httpx.Response:
        url = self._url(path, query)
        attempts = self._max_attempts if method in _SAFE_METHODS or idempotent else 1
        client = self._http()
        attempt = 1
        while True:
            token = await self._token()
            self._last_token = token
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": accept or _DEFAULT_ACCEPT,
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "software-factory",
            }
            content = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                content = json.dumps(body)
            try:
                response = await client.request(method, url, headers=headers, content=content)
            except httpx.TransportError:
                if attempt >= attempts:
                    raise
                await self._sleep(_backoff(attempt))
                attempt += 1
                continue
            if response.is_success:
                return response

            text = response.text
            wait = _retry_delay(response, text, attempt)
            if wait is not None and attempt < attempts:
                await self._sleep(wait)
                attempt += 1
                continue
            path_only = urlsplit(url).path
            raise GitHubError(
                response.status_code,
                self._redact(f"GitHub {method} {path_only} -> {response.status_code}: {text[:300]}"),
                self._redact(text),
            )

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self.transport, timeout=self._timeout)
        return self._client

    def _url(self, path: str, query: Query | None) -> str:
        if re.match(r"^https?://", path):
            # Only follow links back to the same API: a token must never leave for another host.
            if not path.startswith(f"{self.api_url}/"):
                raise ValueError(f"Refusing to send a GitHub token to {urlsplit(path).netloc}")
            url = httpx.URL(path)
        else:
            url = httpx.URL(f"{self.api_url}{path}")
        params = {k: _query_value(v) for k, v in (query or {}).items() if v is not None}
        return str(url.copy_merge_params(params)) if params else str(url)

    def _redact(self, text: str) -> str:
        return text.replace(self._last_token, "***") if self._last_token else text


def _query_value(value: str | int | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _retry_delay(response: httpx.Response, body: str, attempt: int, now: float | None = None) -> float | None:
    """Seconds to wait before retrying, or None when the response isn't worth retrying. Retries 5xx, 429, and
    rate-limited 403s (retry-after, x-ratelimit-remaining: 0, or a secondary rate limit message)."""
    now = time.time() if now is None else now
    status = response.status_code
    headers = response.headers
    retry_after = _parse_retry_after(headers.get("retry-after"), now)
    exhausted = headers.get("x-ratelimit-remaining") == "0"
    secondary = status == 403 and re.search("secondary rate limit", body, re.IGNORECASE) is not None
    rate_limited = status == 429 or (status == 403 and (retry_after is not None or exhausted or secondary))
    if not rate_limited and status < 500:
        return None

    reset = headers.get("x-ratelimit-reset")
    if retry_after is not None:
        wait = retry_after
    elif exhausted and reset:
        try:
            wait = float(reset) - now
        except ValueError:
            return None
    elif secondary:
        # GitHub asks for at least a minute after a secondary limit that names no wait.
        wait = _MAX_WAIT_SECONDS
    else:
        wait = _backoff(attempt)
    if wait > _MAX_WAIT_SECONDS:
        return None
    return max(wait, 0.0)


def _parse_retry_after(value: str | None, now: float) -> float | None:
    """retry-after is delta-seconds or an HTTP date."""
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        return max(parsedate_to_datetime(value).timestamp() - now, 0.0)
    except (TypeError, ValueError):
        return None


def _backoff(attempt: int) -> float:
    return 0.5 * 2 ** (attempt - 1) + random.random() * 0.25  # noqa: S311 - jitter, not security


def _next_link(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(","):
        match = _NEXT_LINK.search(part)
        if match:
            return match.group(1)
    return None


def encode_path(path: str) -> str:
    """Encodes each segment of a git ref or file path, so `release/2.x` stays two segments."""
    return "/".join(quote(segment, safe="") for segment in path.split("/"))
