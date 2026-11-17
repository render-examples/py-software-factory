from __future__ import annotations

import pytest
from render.public_api.models import (
    SandboxCredential,
    SandboxCredentialInjection,
    SandboxCredentialInjectionType,
)

from factory.sandbox.credentials import (
    ANTHROPIC_CREDENTIAL,
    credential_matches,
    ensure_credential,
    list_credentials,
)


def credential(*, path: str = "/v1/messages") -> SandboxCredential:
    return SandboxCredential(
        id="factory-anthropic",
        domain="api.anthropic.com",
        enabled=True,
        methods=["POST"],
        paths=[path],
        injection=SandboxCredentialInjection(
            type_=SandboxCredentialInjectionType.HEADER,
            name="x-api-key",
            template="${SECRET}",
        ),
    )


def test_credential_metadata_pins_domain_method_path_and_header() -> None:
    assert credential_matches(credential(), ANTHROPIC_CREDENTIAL)
    assert not credential_matches(credential(path="/"), ANTHROPIC_CREDENTIAL)


async def test_credential_configuration_rejects_secrets_before_any_api_call() -> None:
    with pytest.raises(ValueError, match="non-empty line"):
        await ensure_credential("sbg-test", ANTHROPIC_CREDENTIAL, "")
    with pytest.raises(ValueError, match="non-empty line"):
        await ensure_credential("sbg-test", ANTHROPIC_CREDENTIAL, "secret\nsecond")


async def test_listing_credentials_requires_an_admin_or_runtime_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RENDER_ADMIN_API_KEY", raising=False)
    monkeypatch.delenv("RENDER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="API_KEY"):
        await list_credentials("sbg-test")
