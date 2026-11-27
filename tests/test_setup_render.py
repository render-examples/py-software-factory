from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from scripts.setup_render import ServiceRef, env_groups, main

DATABASE_URL = "postgresql://factory_user:sup3r-secret@dpg-abc123-a/factory"
RENDER_ENV = ("RENDER_API_KEY", "RENDER_ADMIN_API_KEY", "RENDER_WORKSPACE_ID")


class FakeRenderApi:
    def __init__(
        self,
        services: list[ServiceRef] | None = None,
        env: dict[tuple[str, str], str] | None = None,
        groups: dict[str, str] | None = None,
    ) -> None:
        self.services = services or []
        self.env = env or {}
        self.groups = {"py-factory-workflows": "evg-workflows"} if groups is None else groups
        self.writes: list[tuple[str, str, str]] = []
        self.created: dict[str, dict[str, str]] = {}

    def web_services(self) -> list[ServiceRef]:
        return list(self.services)

    def service_env_var(self, service_id: str, key: str) -> str | None:
        return self.env.get((service_id, key))

    def env_group_id(self, name: str) -> str | None:
        return self.groups.get(name)

    def env_group_keys(self, group_id: str) -> set[str]:
        return {key for group, key, _ in self.writes if group == group_id}

    def set_env_group_var(self, group_id: str, key: str, value: str) -> None:
        self.writes.append((group_id, key, value))

    def create_env_group(self, name: str, values: Mapping[str, str]) -> str:
        group_id = f"evg-new-{name}"
        self.groups[name] = group_id
        self.created[name] = dict(values)
        return group_id


GATEWAY = ServiceRef("srv-gateway", "py-factory-gateway-x7k2")


def _linked_api() -> FakeRenderApi:
    return FakeRenderApi(
        [ServiceRef("srv-other", "docs-site"), GATEWAY, ServiceRef("srv-copy", "my-factory-gateway")],
        env={(GATEWAY.id, "DATABASE_URL"): DATABASE_URL},
    )


def _output(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_link_database_dry_run_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    api = _linked_api()
    assert main(["--link-database"], api=api) == 0
    output = _output(capsys)
    assert api.writes == []
    assert "Dry run" in output
    assert "py-factory-gateway-x7k2 (srv-gateway)" in output
    assert "evg-workflows" in output
    assert DATABASE_URL not in output
    assert "sup3r-secret" not in output


def test_link_database_apply_upserts_the_group(capsys: pytest.CaptureFixture[str]) -> None:
    api = _linked_api()
    assert main(["--link-database", "--apply"], api=api) == 0
    output = _output(capsys)
    assert api.writes == [("evg-workflows", "DATABASE_URL", DATABASE_URL)]
    assert "Redeploy py-factory-workflows" in output
    assert DATABASE_URL not in output
    assert "sup3r-secret" not in output


@pytest.mark.parametrize(
    ("api", "message"),
    [
        (FakeRenderApi([ServiceRef("srv-other", "py-factory-workflows")]), "no py-factory-gateway web service"),
        (
            FakeRenderApi([ServiceRef("srv-a", "py-factory-gateway"), ServiceRef("srv-b", "py-factory-gateway-b2")]),
            "several gateway services match: py-factory-gateway (srv-a), py-factory-gateway-b2 (srv-b)",
        ),
        (FakeRenderApi([GATEWAY]), "py-factory-gateway-x7k2 has no DATABASE_URL; apply the Blueprint first"),
        (
            FakeRenderApi([GATEWAY], env={(GATEWAY.id, "DATABASE_URL"): DATABASE_URL}, groups={}),
            "env group py-factory-workflows not found",
        ),
    ],
)
def test_link_database_failures(api: FakeRenderApi, message: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--link-database", "--apply"], api=api) == 2
    output = _output(capsys)
    assert message in output
    assert api.writes == []
    assert DATABASE_URL not in output


def test_link_database_requires_render_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in RENDER_ENV:
        monkeypatch.delenv(name, raising=False)
    assert main(["--link-database"]) == 2
    assert "RENDER_WORKSPACE_ID" in _output(capsys)


def test_apply_requires_link_database(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--apply"]) == 2
    assert "--link-database" in _output(capsys)


ANTHROPIC_KEY = "sk-ant-api03-very-secret-value"
WEBHOOK_SECRET = "whsec-very-secret-value"


def _env_file(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_env_groups_dry_run_names_keys_and_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = _env_file(
        tmp_path,
        ".env",
        f"RENDER_API_KEY=rnd_key\nANTHROPIC_API_KEY={ANTHROPIC_KEY}\nLOGFIRE_TOKEN=pylf_token\nDATABASE_URL={DATABASE_URL}\n",
    )
    api = FakeRenderApi(groups={})
    assert main(["--env-groups", "--env-file", env], api=api) == 0
    output = _output(capsys)
    assert api.writes == []
    assert api.created == {}
    assert "would create env group py-factory-gateway with RENDER_API_KEY." in output
    assert "would create env group py-factory-workflows with RENDER_API_KEY, ANTHROPIC_API_KEY." in output
    assert "Not in the env files: GITHUB_WEBHOOK_SECRET" in output
    for secret in ("rnd_key", ANTHROPIC_KEY, "pylf_token", "sup3r-secret"):
        assert secret not in output


def test_env_groups_apply_creates_missing_groups_and_upserts_existing_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _env_file(tmp_path, ".env", f"RENDER_API_KEY=rnd_key\nANTHROPIC_API_KEY={ANTHROPIC_KEY}\n")
    app = _env_file(tmp_path, "github-app.env", f"GITHUB_APP_ID=123\nGITHUB_WEBHOOK_SECRET={WEBHOOK_SECRET}\n")
    api = FakeRenderApi()
    assert main(["--env-groups", "--env-file", env, "--env-file", app, "--apply"], api=api) == 0
    output = _output(capsys)
    assert api.created == {"py-factory-gateway": {"RENDER_API_KEY": "rnd_key", "GITHUB_WEBHOOK_SECRET": WEBHOOK_SECRET}}
    assert api.writes == [
        ("evg-workflows", "RENDER_API_KEY", "rnd_key"),
        ("evg-workflows", "GITHUB_APP_ID", "123"),
        ("evg-workflows", "ANTHROPIC_API_KEY", ANTHROPIC_KEY),
    ]
    assert "Created env group py-factory-gateway (evg-new-py-factory-gateway)" in output
    assert WEBHOOK_SECRET not in output
    assert ANTHROPIC_KEY not in output


def test_env_groups_never_copies_logfire_tokens_or_database_urls(tmp_path: Path) -> None:
    env = _env_file(
        tmp_path, ".env", f"LOGFIRE_TOKEN=pylf_token\nDATABASE_URL={DATABASE_URL}\nRENDER_ADMIN_API_KEY=x\n"
    )
    api = FakeRenderApi(groups={"py-factory-gateway": "evg-gateway", "py-factory-workflows": "evg-workflows"})
    assert main(["--env-groups", "--env-file", env, "--apply"], api=api) == 0
    assert api.writes == []


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--env-groups", "--link-database"], "separately"),
        (["--env-file", ".env"], "--env-file is only used with --env-groups"),
        (["--env-groups", "--env-file", "missing.env"], "env file missing.env not found"),
    ],
)
def test_env_groups_argument_errors(argv: list[str], message: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(argv, api=FakeRenderApi()) == 2
    assert message in _output(capsys)


def test_env_groups_reads_wrapped_and_flat_list_items() -> None:
    wrapped = [{"cursor": "c1", "envGroup": {"id": "evg-1", "name": "py-factory-gateway"}}]
    flat = [{"id": "evg-2", "name": "py-factory-workflows"}, "noise", {"cursor": "c3"}]
    assert env_groups(wrapped) == [{"id": "evg-1", "name": "py-factory-gateway"}]
    assert env_groups(flat) == [{"id": "evg-2", "name": "py-factory-workflows"}]
