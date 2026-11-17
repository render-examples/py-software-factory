from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Provider:
    key_env: str
    api_host: str


PROVIDERS = MappingProxyType(
    {
        "anthropic": Provider(key_env="ANTHROPIC_API_KEY", api_host="api.anthropic.com"),
    }
)


def provider_for_model(model: str) -> Provider:
    provider_name, separator, _ = model.partition(":")
    if not separator or provider_name not in PROVIDERS:
        raise ValueError(f"unsupported model provider in {model!r}")
    return PROVIDERS[provider_name]


def model_hosts(models: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(provider_for_model(model).api_host for model in models))
