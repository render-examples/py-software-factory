from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

from scripts.common import parse_env_file
from scripts.setup_render import ServiceRef, ensure_events_read_token, main

DATABASE_URL = "postgresql://factory_user:sup3r-secret@dpg-abc123-a/factory"
RENDER_ENV = ("RENDER_API_KEY", "RENDER_ADMIN_API_KEY", "RENDER_WORKSPACE_ID")


class FakeRenderApi:
    def __init__(
        self,
        services: list[ServiceRef],
        env: dict[tuple[str, str], str] | None = None,
        groups: dict[str, str] | None = None,
    ) -> None:
        self.services = services
        self.env = env or {}
        self.groups = {"factory-workflows": "evg-workflows"} if groups is None else groups
        self.writes: list[tuple[str, str, str]] = []

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


GATEWAY = ServiceRef("srv-gateway", "factory-gateway-x7k2")


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
    assert "factory-gateway-x7k2 (srv-gateway)" in output
    assert "evg-workflows" in output
    assert DATABASE_URL not in output
    assert "sup3r-secret" not in output


def test_link_database_apply_upserts_the_group(capsys: pytest.CaptureFixture[str]) -> None:
    api = _linked_api()
    assert main(["--link-database", "--apply"], api=api) == 0
    output = _output(capsys)
    assert api.writes == [("evg-workflows", "DATABASE_URL", DATABASE_URL)]
    assert "Redeploy factory-workflows" in output
    assert DATABASE_URL not in output
    assert "sup3r-secret" not in output


@pytest.mark.parametrize(
    ("api", "message"),
    [
        (FakeRenderApi([ServiceRef("srv-other", "factory-workflows")]), "no factory-gateway web service"),
        (
            FakeRenderApi([ServiceRef("srv-a", "factory-gateway"), ServiceRef("srv-b", "factory-gateway-b2")]),
            "several gateway services match: factory-gateway (srv-a), factory-gateway-b2 (srv-b)",
        ),
        (FakeRenderApi([GATEWAY]), "factory-gateway-x7k2 has no DATABASE_URL; apply the Blueprint first"),
        (
            FakeRenderApi([GATEWAY], env={(GATEWAY.id, "DATABASE_URL"): DATABASE_URL}, groups={}),
            "env group factory-workflows not found",
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


def test_setup_generates_events_read_token_without_printing_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("RENDER_WORKSPACE_ID=tea-123\nEVENTS_READ_TOKEN=\nGITHUB_APP_ID=42")
    env_file.chmod(0o644)

    assert main(["--group-id", "sbg-abc123", "--env-file", str(env_file)]) == 0

    values = parse_env_file(env_file.read_text())
    token = values["EVENTS_READ_TOKEN"]
    assert re.fullmatch(r"[0-9a-f]{64}", token)
    assert values["RENDER_WORKSPACE_ID"] == "tea-123"
    assert values["GITHUB_APP_ID"] == "42"
    assert env_file.read_text().count("EVENTS_READ_TOKEN") == 1
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    output = _output(capsys)
    assert token not in output
    assert "Generated EVENTS_READ_TOKEN" in output
    assert "factory-gateway env group" in output


def test_events_read_token_is_kept_when_present(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("EVENTS_READ_TOKEN=existing\n")
    assert not ensure_events_read_token(env_file)
    assert env_file.read_text() == "EVENTS_READ_TOKEN=existing\n"


def test_events_read_token_creates_or_appends(tmp_path: Path) -> None:
    created = tmp_path / "new" / ".env"
    assert ensure_events_read_token(created)
    assert re.fullmatch(r"EVENTS_READ_TOKEN=[0-9a-f]{64}\n", created.read_text())

    appended = tmp_path / ".env"
    appended.write_text("PORT=3000")
    assert ensure_events_read_token(appended)
    assert set(parse_env_file(appended.read_text())) == {"PORT", "EVENTS_READ_TOKEN"}


def test_link_database_does_not_touch_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    assert main(["--link-database", "--env-file", str(env_file)], api=_linked_api()) == 0
    assert not env_file.exists()
