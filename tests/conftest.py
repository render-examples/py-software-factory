from __future__ import annotations

import pytest

import factory.config
from factory.github.auth import reset_token_cache
from factory.github.client import reset_bot_login_cache
from tests.fixtures import TEST_CONFIG

pytest_plugins = ["tests.db"]


@pytest.fixture(autouse=True)
def _test_config(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    monkeypatch.setattr(factory.config, "_config", TEST_CONFIG)


@pytest.fixture(autouse=True)
def _reset_caches() -> None:  # pyright: ignore[reportUnusedFunction]
    reset_token_cache()
    reset_bot_login_cache()
