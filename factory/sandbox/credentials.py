"""Sandbox-group credential brokering through Render's generated API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import cast

from render import RenderAsync
from render.public_api.api.sandboxes import (
    create_sandbox_group_credential,
    list_sandbox_group_credentials,
    update_sandbox_group_credential,
)
from render.public_api.client import AuthenticatedClient
from render.public_api.models import (
    Error,
    SandboxCredential,
    SandboxCredentialCreate,
    SandboxCredentialInjection,
    SandboxCredentialInjectionType,
    SandboxCredentialUpdate,
)


@dataclass(frozen=True)
class BrokeredCredential:
    id: str
    domain: str
    header: str
    methods: tuple[str, ...] = ("POST",)
    paths: tuple[str, ...] = ("/v1/messages",)


ANTHROPIC_CREDENTIAL = BrokeredCredential(
    id="factory-anthropic",
    domain="api.anthropic.com",
    header="x-api-key",
)


async def list_credentials(
    sandbox_group_id: str,
    *,
    admin_api_key: str | None = None,
) -> list[SandboxCredential]:
    """Return credential metadata; Render never returns secret values."""
    result = await list_sandbox_group_credentials.asyncio(
        sandbox_group_id,
        client=_client(admin_api_key),
    )
    if isinstance(result, Error):
        raise RuntimeError(f"could not list sandbox credentials: {result}")
    if result is None:
        raise RuntimeError("Render returned no sandbox credential list")
    return result


async def ensure_credential(
    sandbox_group_id: str,
    credential: BrokeredCredential,
    secret: str,
    *,
    admin_api_key: str | None = None,
) -> SandboxCredential:
    """Create or rotate one restricted credential without exposing its value."""
    if not secret or any(character in secret for character in "\r\n"):
        raise ValueError("brokered credential secret must be one non-empty line")
    existing = next(
        (
            item
            for item in await list_credentials(sandbox_group_id, admin_api_key=admin_api_key)
            if item.id == credential.id
        ),
        None,
    )
    injection = SandboxCredentialInjection(
        type_=SandboxCredentialInjectionType.HEADER,
        name=credential.header,
        template="${SECRET}",
    )
    if existing is None:
        result = await create_sandbox_group_credential.asyncio(
            sandbox_group_id,
            client=_client(admin_api_key),
            body=SandboxCredentialCreate(
                id=credential.id,
                domain=credential.domain,
                value=secret,
                injection=injection,
                methods=list(credential.methods),
                paths=list(credential.paths),
            ),
        )
    else:
        result = await update_sandbox_group_credential.asyncio(
            sandbox_group_id,
            credential.id,
            client=_client(admin_api_key),
            body=SandboxCredentialUpdate(
                value=secret,
                domain=credential.domain,
                enabled=True,
                methods=list(credential.methods),
                paths=list(credential.paths),
                injection=injection,
            ),
        )
    if isinstance(result, Error):
        raise RuntimeError(f"could not configure sandbox credential {credential.id}: {result}")
    if not isinstance(result, SandboxCredential):
        raise RuntimeError(f"Render returned no metadata for sandbox credential {credential.id}")
    return result


def credential_matches(actual: SandboxCredential, expected: BrokeredCredential) -> bool:
    """Whether non-secret metadata preserves the intended spend boundary."""
    return (
        actual.id == expected.id
        and actual.domain == expected.domain
        and actual.enabled
        and tuple(actual.methods) == expected.methods
        and tuple(actual.paths) == expected.paths
        and actual.injection.type_ == SandboxCredentialInjectionType.HEADER
        and actual.injection.name == expected.header
    )


def _client(api_key: str | None) -> AuthenticatedClient:
    token = (api_key or os.environ.get("RENDER_ADMIN_API_KEY") or os.environ.get("RENDER_API_KEY") or "").strip()
    if not token:
        raise RuntimeError("RENDER_ADMIN_API_KEY or RENDER_API_KEY is required for sandbox credentials")
    return cast(AuthenticatedClient, RenderAsync(token=token)._client.internal)
