from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from factory.github import auth
from factory.github.auth import github_token

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_KEY = _KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
PUBLIC_KEY = _KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


class TokenEndpoint:
    def __init__(self, *, status: int = 201, expires_in: float = 3600) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.expires_in = expires_in
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 201:
            return httpx.Response(self.status, text="Bad credentials")
        expires = datetime.fromtimestamp(auth._now() + self.expires_in, UTC).isoformat()
        return httpx.Response(201, json={"token": f"ghs_token_{len(self.requests)}", "expires_at": expires})


@pytest.fixture(autouse=True)
def app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", PRIVATE_KEY)
    monkeypatch.delenv("GITHUB_API_URL", raising=False)


async def test_mints_a_token_restricted_to_one_repository_with_an_rs256_app_jwt() -> None:
    endpoint = TokenEndpoint()
    token = await github_token(99, "web", {"contents": "read"}, transport=endpoint.transport)
    assert token == "ghs_token_1"
    assert len(endpoint.requests) == 1
    request = endpoint.requests[0]
    assert str(request.url) == "https://api.github.com/app/installations/99/access_tokens"
    assert json.loads(request.content) == {"repositories": ["web"], "permissions": {"contents": "read"}}

    encoded = request.headers["authorization"].removeprefix("Bearer ")
    assert jwt.get_unverified_header(encoded) == {"alg": "RS256", "typ": "JWT"}
    claims: dict[str, Any] = jwt.decode(encoded, PUBLIC_KEY, algorithms=["RS256"])
    now = int(time.time())
    assert claims["iss"] == "12345"
    assert claims["iat"] <= now - 59
    assert claims["exp"] - claims["iat"] <= 600


async def test_caches_per_installation_repository_and_permission_set() -> None:
    endpoint = TokenEndpoint()
    t = endpoint.transport
    a = await github_token(1, "web", {"contents": "read", "metadata": "read"}, transport=t)
    b = await github_token(1, "web", {"metadata": "read", "contents": "read"}, transport=t)
    assert b == a
    assert len(endpoint.requests) == 1
    await github_token(1, "api", {"contents": "read", "metadata": "read"}, transport=t)
    await github_token(2, "web", {"contents": "read", "metadata": "read"}, transport=t)
    await github_token(1, "web", {"contents": "write"}, transport=t)
    await github_token(1, "web", transport=t)
    assert len(endpoint.requests) == 5


async def test_renews_a_token_within_five_minutes_of_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1_800_000_000.0]
    monkeypatch.setattr(auth, "_now", lambda: clock[0])
    endpoint = TokenEndpoint(expires_in=10 * 60)
    await github_token(1, "web", transport=endpoint.transport)
    clock[0] += 4 * 60
    await github_token(1, "web", transport=endpoint.transport)
    assert len(endpoint.requests) == 1
    clock[0] += 2 * 60
    assert await github_token(1, "web", transport=endpoint.transport) == "ghs_token_2"
    assert len(endpoint.requests) == 2


async def test_a_failed_exchange_raises_without_caching() -> None:
    endpoint = TokenEndpoint(status=401)
    with pytest.raises(RuntimeError, match=r"token exchange failed \(401\)"):
        await github_token(1, "web", transport=endpoint.transport)


def test_accepts_the_private_key_escaped_or_base64_encoded() -> None:
    import base64

    assert auth.normalize_private_key(PRIVATE_KEY.replace("\n", "\\n")) == PRIVATE_KEY
    assert auth.normalize_private_key(base64.b64encode(PRIVATE_KEY.encode()).decode()) == PRIVATE_KEY
