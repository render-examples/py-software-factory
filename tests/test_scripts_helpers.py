from __future__ import annotations

import inspect

import pytest

import scripts.doctor as doctor
from scripts.common import Check, normalize_gateway_url, parse_env_file, redact, validate_repository
from scripts.doctor import _logfire_checks, _workflows_database_check
from scripts.preflight import blueprint_checks
from scripts.setup_app import app_manifest, manifest_form
from scripts.setup_render import ServiceRef, render_handoff


def test_parse_env_file_handles_export_quotes_and_comments() -> None:
    assert parse_env_file(
        """
        # local values
        export RENDER_WORKSPACE_ID=tea-123
        GITHUB_APP_PRIVATE_KEY="line one\\nline two"
        EMPTY=
        """
    ) == {
        "RENDER_WORKSPACE_ID": "tea-123",
        "GITHUB_APP_PRIVATE_KEY": "line one\\nline two",
        "EMPTY": "",
    }


@pytest.mark.parametrize("line", ["NO_EQUALS", "1BAD=value", 'KEY="unterminated'])
def test_parse_env_file_rejects_malformed_lines(line: str) -> None:
    with pytest.raises(ValueError, match="line 1"):
        parse_env_file(line)


def test_gateway_and_repository_validation() -> None:
    assert normalize_gateway_url("https://factory.example.com/") == "https://factory.example.com"
    assert normalize_gateway_url("http://localhost:3000/") == "http://localhost:3000"
    assert validate_repository("render-examples/target.py") == "render-examples/target.py"
    with pytest.raises(ValueError, match="HTTPS"):
        normalize_gateway_url("http://factory.example.com")
    with pytest.raises(ValueError, match="OWNER/NAME"):
        validate_repository("https://github.com/owner/repo")
    for invalid in ("../repo", "owner/..", "ownér/repo"):
        with pytest.raises(ValueError, match="OWNER/NAME"):
            validate_repository(invalid)


def test_manifest_pins_events_permissions_and_webhook() -> None:
    manifest = app_manifest("factory", "https://factory.example.com/")
    assert manifest["hook_attributes"] == {
        "url": "https://factory.example.com/webhooks/github",
        "active": True,
    }
    assert manifest["default_events"] == ["issues", "issue_comment", "pull_request", "push"]
    assert manifest["default_permissions"]["contents"] == "write"
    assert manifest["default_permissions"]["members"] == "read"
    form = manifest_form(manifest, state="csrf-token", organization="acme")
    assert 'action="https://github.com/organizations/acme/settings/apps/new?state=csrf-token"' in form
    assert 'name="manifest"' in form


def test_blueprint_contract_checks_current_file() -> None:
    checks = blueprint_checks(
        """
services:
  - type: web
    envVars:
      - fromGroup: py-factory-gateway
      - key: DATABASE_URL
        fromDatabase:
          name: py-factory-db
          property: connectionString
  - type: workflow
    runtime: python
    buildCommand: uv sync --frozen --no-dev
    envVars:
      - fromGroup: py-factory-workflows
      - key: RENDER_WORKFLOW_SLUG
        value: py-factory-workflows
databases:
  - name: py-factory-db
    plan: basic-256mb
    ipAllowList: []
"""
    )
    assert all(check.ok for check in checks), checks


def test_blueprint_checks_report_invalid_yaml() -> None:
    [check] = blueprint_checks("services: [")
    assert not check.ok
    assert check.name == "blueprint YAML"


def test_render_handoff_never_places_admin_key_in_runtime() -> None:
    handoff = "\n".join(render_handoff("sbg-abc123"))
    assert "FACTORY_SANDBOX_GROUP_ID=sbg-abc123" in handoff
    assert "Never put RENDER_ADMIN_API_KEY" in handoff
    assert "LOGFIRE_TOKEN" in handoff
    assert "--link-database --apply" in handoff


def test_redact_replaces_longest_secret_first() -> None:
    text = "postgresql://user:pw@host/db failed for user:pw"
    assert redact(text, ["pw", "postgresql://user:pw@host/db", "", "user:pw"]) == ("[redacted] failed for [redacted]")


class _GroupApi:
    def __init__(self, keys: set[str], *, group_id: str | None = "evg-workflows") -> None:
        self.keys = keys
        self.group_id = group_id

    def web_services(self) -> list[ServiceRef]:
        return []

    def service_env_var(self, service_id: str, key: str) -> str | None:
        return None

    def env_group_id(self, name: str) -> str | None:
        assert name == "py-factory-workflows"
        return self.group_id

    def env_group_keys(self, group_id: str) -> set[str]:
        return self.keys

    def set_env_group_var(self, group_id: str, key: str, value: str) -> None:
        raise AssertionError("doctor never writes")


def test_doctor_workflows_database_check_points_at_link_step() -> None:
    missing = _workflows_database_check({}, _GroupApi({"RENDER_API_KEY"}))
    assert not missing.ok
    assert "setup-render --link-database --apply" in missing.detail
    assert "redeploy py-factory-workflows" in missing.detail
    present = _workflows_database_check({}, _GroupApi({"DATABASE_URL"}))
    assert present.ok
    assert present.detail == "set in env group py-factory-workflows"
    no_group = _workflows_database_check({}, _GroupApi(set(), group_id=None))
    assert not no_group.ok
    assert "apply the Blueprint" in no_group.detail


def test_doctor_workflows_database_check_needs_render_credentials() -> None:
    check = _workflows_database_check({})
    assert not check.ok
    assert "RENDER_WORKSPACE_ID" in check.detail


def test_doctor_database_section_adds_local_url_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(environment: dict[str, str]) -> list[Check]:
        return [
            Check("database connects", False, "OperationalError: refused"),
            Check("migrations current", False, "skipped: database unreachable", required=False),
        ]

    monkeypatch.setattr(doctor, "database_checks", unreachable)
    connects, migrations = doctor._database_section({"DATABASE_URL": "postgresql:///x"})
    assert "sslmode=verify-full&sslrootcert=system" in connects.detail
    assert migrations.detail == "skipped: database unreachable"


class GroupKeys:
    def __init__(self, keys: dict[str, set[str]]) -> None:
        self.keys = keys

    def web_services(self) -> list[ServiceRef]:
        return []

    def service_env_var(self, service_id: str, key: str) -> str | None:
        return None

    def env_group_id(self, name: str) -> str | None:
        return f"evg-{name}" if name in self.keys else None

    def env_group_keys(self, group_id: str) -> set[str]:
        return self.keys[group_id.removeprefix("evg-")]

    def set_env_group_var(self, group_id: str, key: str, value: str) -> None:
        raise AssertionError("doctor never writes")


def test_doctor_reports_which_services_send_traces_without_requiring_it() -> None:
    api = GroupKeys({"py-factory-gateway": {"LOGFIRE_TOKEN"}, "py-factory-workflows": {"DATABASE_URL"}})
    gateway, workflows = _logfire_checks({}, api)
    assert (gateway.name, gateway.ok, gateway.required) == ("py-factory-gateway LOGFIRE_TOKEN", True, False)
    assert (workflows.ok, workflows.required) == (False, False)
    assert "sends no traces" in workflows.detail


def test_doctor_uses_one_event_loop_for_remote_checks() -> None:
    assert inspect.getsource(doctor.main).count("asyncio.run(") == 1
