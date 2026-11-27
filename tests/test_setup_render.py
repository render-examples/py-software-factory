from __future__ import annotations

import pytest

from scripts.setup_render import ServiceRef, main

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
        self.groups = {"py-factory-workflows": "evg-workflows"} if groups is None else groups
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
