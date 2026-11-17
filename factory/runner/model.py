from __future__ import annotations

import ssl
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

import httpx2
import truststore
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelName
from pydantic_ai.providers.anthropic import AnthropicProvider

from factory.agents.providers import provider_for_model

BROKERED_PLACEHOLDER_KEY = "render-brokered-credential"


async def _strip_anthropic_key(request: httpx2.Request) -> None:
    """Leave the authentication header absent so Render's broker is its only source."""
    request.headers.pop("x-api-key", None)


@asynccontextmanager
async def brokered_model(model: str) -> AsyncGenerator[Model]:
    """Own a direct Anthropic model and close its externally supplied HTTP client."""
    provider = provider_for_model(model)
    provider_name, _, model_name = model.partition(":")
    if provider_name != "anthropic":
        raise ValueError(f"brokered provider {provider_name!r} is not supported")
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client = httpx2.AsyncClient(
        base_url=f"https://{provider.api_host}",
        verify=context,
        event_hooks={"request": [_strip_anthropic_key]},
    )
    try:
        anthropic = AnthropicProvider(
            api_key=BROKERED_PLACEHOLDER_KEY,
            base_url=f"https://{provider.api_host}",
            http_client=client,
        )
        yield AnthropicModel(cast(AnthropicModelName, model_name), provider=anthropic)
    finally:
        await client.aclose()
