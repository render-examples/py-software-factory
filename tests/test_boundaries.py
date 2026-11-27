from __future__ import annotations

import json
import subprocess
import sys

import pytest


def loaded_by(module: str) -> set[str]:
    code = f"import json, sys; import {module}; print(json.dumps(sorted(sys.modules)))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, check=True, text=True).stdout
    return set(json.loads(out))


def factory_modules_loaded_by(module: str) -> set[str]:
    return {m for m in loaded_by(module) if m == "factory" or m.startswith("factory.")}


GATEWAY_FORBIDDEN = (
    "factory.github",
    "factory.sandbox",
    "factory.agents",
    "factory.runs",
    "factory.changeset",
    "factory.stages",
    "factory.workflows",
)


@pytest.mark.parametrize("module", ["factory.gateway", "factory.gateway.main", "factory.tracing"])
def test_the_gateway_holds_no_github_credential_and_runs_no_model(module: str) -> None:
    loaded = loaded_by(module)
    assert sorted(m for m in loaded if m.startswith(GATEWAY_FORBIDDEN)) == []
    assert "pydantic_ai" not in loaded


@pytest.mark.parametrize("module", ["factory.db", "factory.events", "factory.threads"])
def test_the_event_log_loads_nothing_the_gateway_must_not_hold(module: str) -> None:
    loaded = factory_modules_loaded_by(module)
    assert sorted(m for m in loaded if m.startswith(GATEWAY_FORBIDDEN)) == []


def test_agents_never_load_the_event_log_or_run_ownership() -> None:
    loaded = factory_modules_loaded_by("factory.agents")
    assert sorted(m for m in loaded if m.startswith(("factory.db", "factory.runs", "factory.changeset"))) == []


def test_schemas_import_no_host_only_code() -> None:
    assert factory_modules_loaded_by("factory.schemas") <= {
        "factory",
        "factory.identifiers",
        "factory.schemas",
        "factory.schemas.base",
        "factory.schemas.implementation",
        "factory.schemas.review",
        "factory.schemas.spec",
        "factory.schemas.tasks",
        "factory.schemas.triage",
    }
