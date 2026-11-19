"""Dependency rules that keep credentials and model-directed code where they belong."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest


def modules_loaded_by(module: str) -> set[str]:
    code = f"import json, sys; import {module}; print(json.dumps(sorted(sys.modules)))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, check=True, text=True).stdout
    return {m for m in json.loads(out) if m == "factory" or m.startswith("factory.")}


GATEWAY_FORBIDDEN = (
    "factory.github",
    "factory.sandbox",
    "factory.agents",
    "factory.runner",
    "factory.runs",
    "factory.changeset",
    "factory.stages",
    "factory.workflows",
)


@pytest.mark.parametrize(
    "module", ["factory.gateway", "factory.gateway.main", "factory.gateway.hub", "factory.gateway.stream"]
)
def test_the_gateway_holds_no_github_credential_and_runs_no_model(module: str) -> None:
    """Only the Workflows service writes to GitHub (invariant 3); the gateway only verifies and starts runs."""
    loaded = modules_loaded_by(module)
    assert sorted(m for m in loaded if m.startswith(GATEWAY_FORBIDDEN)) == []


@pytest.mark.parametrize("module", ["factory.db", "factory.events", "factory.threads"])
def test_the_event_log_loads_nothing_the_gateway_must_not_hold(module: str) -> None:
    """The gateway records deliveries in the event log, so the log must stay clear of credentials and agents."""
    loaded = modules_loaded_by(module)
    assert sorted(m for m in loaded if m.startswith(GATEWAY_FORBIDDEN)) == []


@pytest.mark.parametrize("module", ["factory.agents", "factory.runner.main"])
def test_code_shipped_in_the_runner_never_loads_the_event_log(module: str) -> None:
    """The runner zip packages factory.agents and factory.runner; Postgres access must stay on the host."""
    loaded = modules_loaded_by(module)
    assert sorted(m for m in loaded if m.startswith(("factory.db", "factory.agent_runs.record"))) == []


def test_schemas_import_no_host_only_code() -> None:
    """The in-sandbox runner imports the schemas, so they must not pull in GitHub, sandbox, claims, or config."""
    assert modules_loaded_by("factory.schemas") <= {
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
