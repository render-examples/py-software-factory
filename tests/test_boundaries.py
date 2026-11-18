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
    "factory.claims",
    "factory.changeset",
    "factory.stages",
    "factory.workflows",
)


@pytest.mark.parametrize("module", ["factory.gateway", "factory.gateway.main"])
def test_the_gateway_holds_no_github_credential_and_runs_no_model(module: str) -> None:
    """Only the Workflows service writes to GitHub (invariant 3); the gateway only verifies and starts runs."""
    loaded = modules_loaded_by(module)
    assert sorted(m for m in loaded if m.startswith(GATEWAY_FORBIDDEN)) == []


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
