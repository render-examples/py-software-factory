from __future__ import annotations

import pytest

from scripts.common import normalize_gateway_url, parse_env_file, validate_repository
from scripts.preflight import blueprint_checks
from scripts.setup_app import app_manifest, manifest_form
from scripts.setup_render import render_handoff


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
      - fromGroup: factory-gateway
  - type: workflow
    runtime: python
    buildCommand: uv sync --frozen --no-dev
    envVars:
      - fromGroup: factory-workflows
"""
    )
    assert all(check.ok for check in checks), checks


def test_render_handoff_never_places_admin_key_in_runtime() -> None:
    handoff = "\n".join(render_handoff("sbg-abc123"))
    assert "FACTORY_SANDBOX_GROUP_ID=sbg-abc123" in handoff
    assert "Never put RENDER_ADMIN_API_KEY" in handoff
    assert "POST /v1/messages" in handoff
