from __future__ import annotations

import json
import re

import pytest
from pydantic import ValidationError

from factory.config import require_repo, resolve_repo
from factory.config_model import FactoryConfig, RepoConfig
from tests.fixtures import REPO, TEST_CONFIG


def with_egress(egress: tuple[str, ...] | str) -> FactoryConfig:
    repo = {"base_branch": "main", "verify": (), "egress": egress}
    return FactoryConfig.model_validate({**TEST_CONFIG.model_dump(), "repos": {"acme/web": repo}})


class TestEgressConfiguration:
    def test_accepts_hostnames_a_leftmost_wildcard_and_allow_all(self) -> None:
        with_egress(("registry.npmjs.org", "*.pkg.dev", "proxy.golang.org"))
        with_egress("allow-all")

    @pytest.mark.parametrize(
        "host",
        [
            "https://registry.npmjs.org",
            "registry.npmjs.org/",
            "registry.npmjs.org:443",
            "Registry.npmjs.org",
            "localhost",
            "api.*.example.com",
            "",
        ],
    )
    def test_rejects_a_bad_host_at_construction_naming_the_repository(self, host: str) -> None:
        expected = f'factory_config.py repos["acme/web"]: egress host {json.dumps(host)}'
        with pytest.raises(ValidationError, match=re.escape(expected)):
            with_egress((host,))


def test_the_deployment_config_is_valid() -> None:
    import factory_config

    assert factory_config.CONFIG.repos


def test_rejects_unknown_keys_so_a_typo_fails_at_startup() -> None:
    with pytest.raises(ValidationError):
        RepoConfig.model_validate({"base_branch": "main", "verify": (), "verfy": ()})


def test_resolves_only_configured_repositories() -> None:
    repo = resolve_repo(REPO, 7)
    assert repo is not None
    assert (repo.owner, repo.name, repo.installation_id) == ("acme", "widget", 7)
    assert resolve_repo("someone/else", 7) is None
    with pytest.raises(ValueError, match="not configured"):
        require_repo("someone/else", 7)
