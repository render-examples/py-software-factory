from __future__ import annotations

from typing import Any, cast

import pytest
import yaml

import scripts.preflight as preflight
from scripts.common import ROOT, Check
from scripts.preflight import blueprint_checks


def _blueprint() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((ROOT / "render.yaml").read_text()))


def _failed(document: dict[str, Any]) -> set[str]:
    return {check.name for check in blueprint_checks(yaml.safe_dump(document)) if not check.ok}


def test_repository_blueprint_passes() -> None:
    checks = blueprint_checks((ROOT / "render.yaml").read_text())
    assert all(check.ok for check in checks), [check for check in checks if not check.ok]
    assert {"blueprint database", "workflow database", "database plan", "database access"} <= {
        check.name for check in checks
    }


def test_workflow_database_reference_fails() -> None:
    document = _blueprint()
    workflow = document["services"][1]
    workflow["envVars"].append(
        {"key": "DATABASE_URL", "fromDatabase": {"name": "py-factory-db", "property": "connectionString"}}
    )
    assert _failed(document) == {"workflow database"}


def test_workflow_static_database_url_fails() -> None:
    document = _blueprint()
    document["services"][1]["envVars"].append({"key": "DATABASE_URL", "value": "postgresql://example"})
    assert _failed(document) == {"workflow database"}


def test_missing_database_or_gateway_link_fails() -> None:
    without_database = _blueprint()
    del without_database["databases"]
    assert {"blueprint database", "database plan", "database access"} <= _failed(without_database)

    without_link = _blueprint()
    gateway = without_link["services"][0]
    gateway["envVars"] = [entry for entry in gateway["envVars"] if entry.get("key") != "DATABASE_URL"]
    assert _failed(without_link) == {"blueprint database"}


@pytest.mark.parametrize(
    ("field", "value", "failed"),
    [
        ("plan", "free", "database plan"),
        ("plan", None, "database plan"),
        ("ipAllowList", [{"source": "0.0.0.0/0", "description": "everywhere"}], "database access"),
    ],
)
def test_database_settings_are_checked(field: str, value: object, failed: str) -> None:
    document = _blueprint()
    if value is None:
        del document["databases"][0][field]
    else:
        document["databases"][0][field] = value
    assert _failed(document) == {failed}


def test_missing_workflow_slug_fails() -> None:
    document = _blueprint()
    workflow = document["services"][1]
    workflow["envVars"] = [entry for entry in workflow["envVars"] if entry.get("key") != "RENDER_WORKFLOW_SLUG"]
    assert _failed(document) == {"workflow slug"}


def test_env_lists_include_the_database() -> None:
    assert "DATABASE_URL" in preflight.GATEWAY_ENV
    assert "DATABASE_URL" in preflight.WORKFLOW_ENV


def test_unset_database_url_is_a_non_blocking_skip() -> None:
    checks = preflight.local_checks({})
    [skip] = [check for check in checks if check.name == "database"]
    assert not skip.ok
    assert not skip.required
    assert "docs/configuration.md#database" in skip.detail


def test_set_database_url_runs_database_check(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake(environment: dict[str, str]) -> list[Check]:
        seen.append(environment["DATABASE_URL"])
        return [Check("database connects", True, "select 1 succeeded")]

    monkeypatch.setattr(preflight, "database_checks", fake)
    checks = preflight.local_checks({"DATABASE_URL": "postgresql:///factory"})
    assert seen == ["postgresql:///factory"]
    assert Check("database connects", True, "select 1 succeeded") in checks
    names = {check.name for check in preflight.local_checks({}, database=False)}
    assert not names & {"database", "database connects", "migrations current"}
