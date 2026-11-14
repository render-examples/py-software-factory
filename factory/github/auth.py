"""GitHub App authentication: the App JWT and repository-scoped installation tokens."""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

import httpx
import jwt

from factory.github._json import get

_REFRESH_MARGIN_SECONDS = 5 * 60

_token_cache: dict[str, tuple[str, float]] = {}


def _now() -> float:
    """Test seam."""
    return time.time()


def github_api_url(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return (env.get("GITHUB_API_URL", "").strip() or "https://api.github.com").rstrip("/")


@dataclass(frozen=True)
class AppCredentials:
    app_id: str
    private_key: str


def app_credentials() -> AppCredentials:
    app_id = os.environ.get("GITHUB_APP_ID", "").strip()
    private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY", "").strip()
    if not app_id or not private_key:
        raise RuntimeError("GitHub App credentials required: set GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY")
    return AppCredentials(app_id, normalize_private_key(private_key))


def normalize_private_key(value: str) -> str:
    """Accepts the PEM as-is, with escaped newlines, or base64-encoded: env vars make real newlines awkward and
    every deployment tool escapes them differently."""
    if "BEGIN" in value:
        return value.replace("\\n", "\n")
    return base64.b64decode(value).decode()


def app_jwt(credentials: AppCredentials | None = None) -> str:
    """A JWT signed with the App's private key, valid for nine minutes (backdated one for clock skew)."""
    credentials = credentials or app_credentials()
    now = int(_now())
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": credentials.app_id}, credentials.private_key, "RS256")


def _cache_key(installation_id: int, repo: str, permissions: Mapping[str, str] | None) -> str:
    perms = ",".join(f"{k}:{v}" for k, v in sorted(permissions.items())) if permissions is not None else "*"
    return f"{installation_id}/{repo}/{perms}"


async def github_token(
    installation_id: int,
    repo: str,
    permissions: Mapping[str, str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Mints an installation token restricted to one repository (`repo` is the name without the owner), cached per
    (installation, repository, permissions) until five minutes before it expires. Omit `permissions` for every
    permission the installation has."""
    key = _cache_key(installation_id, repo, permissions)
    cached = _token_cache.get(key)
    if cached and cached[1] - _REFRESH_MARGIN_SECONDS > _now():
        return cached[0]

    credentials = app_credentials()
    body: dict[str, object] = {"repositories": [repo]}
    if permissions is not None:
        body["permissions"] = dict(permissions)
    async with httpx.AsyncClient(transport=transport, timeout=15) as client:
        response = await client.post(
            f"{github_api_url()}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt(credentials)}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "software-factory",
                "Content-Type": "application/json",
            },
            content=json.dumps(body),
        )
    if not response.is_success:
        raise RuntimeError(f"GitHub App token exchange failed ({response.status_code}): {response.text[:300]}")
    result = response.json()
    token = get(result, "token")
    expires_at = _parse_time(get(result, "expires_at"))
    if not isinstance(token, str) or not token or expires_at is None:
        raise RuntimeError("GitHub App token exchange returned no token or expiry")
    _token_cache[key] = (token, expires_at)
    return token


def reset_token_cache() -> None:
    """Test seam."""
    _token_cache.clear()


def _parse_time(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None
