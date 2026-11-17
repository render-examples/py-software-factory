"""Local deployment preflight. This command never calls Render or GitHub."""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from scripts.common import ROOT, Check, command_check, load_environment, print_checks, require_values

GATEWAY_ENV = ("RENDER_API_KEY", "GITHUB_WEBHOOK_SECRET")
WORKFLOW_ENV = (
    "RENDER_API_KEY",
    "RENDER_WORKSPACE_ID",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "ANTHROPIC_API_KEY",
    "FACTORY_SANDBOX_GROUP_ID",
)


def blueprint_checks(text: str) -> list[Check]:
    """Check the security- and contract-bearing parts without needing a YAML library."""
    workflow = text.split("- type: workflow", 1)[1] if "- type: workflow" in text else ""
    workflow = workflow.split("\n  - type:", 1)[0]
    service_types = [line.strip() for line in text.splitlines() if line.strip().startswith("- type:")]
    return [
        Check("blueprint services", service_types == ["- type: web", "- type: workflow"], repr(service_types)),
        Check("blueprint database", "\ndatabases:" not in text, "no database declared"),
        Check("workflow runtime", "runtime: python" in workflow, "Python runtime"),
        Check("workflow build", "uv sync --frozen --no-dev" in workflow, "locked uv build"),
        Check("workflow plan", "\n    plan:" not in workflow, "service-level plan omitted"),
        Check("secret groups", text.count("fromGroup: factory-") == 2, "gateway and workflow groups linked"),
    ]


def local_checks(environment: dict[str, str], *, require_secrets: bool = False) -> list[Check]:
    checks = [
        Check("python", sys.version_info >= (3, 11), sys.version.split()[0]),
        Check("uv.lock", (ROOT / "uv.lock").is_file(), "present" if (ROOT / "uv.lock").is_file() else "missing"),
        Check(
            "workflow entrypoint",
            (ROOT / "factory/workflows/app.py").is_file(),
            "factory/workflows/app.py",
            required=require_secrets,
        ),
        command_check(("uv", "--version")),
        command_check(("shellcheck", "--version")),
        command_check(("render", "--version"), required=False),
    ]
    blueprint = ROOT / "render.yaml"
    checks.append(Check("render.yaml", blueprint.is_file(), str(blueprint)))
    if blueprint.is_file():
        checks.extend(blueprint_checks(blueprint.read_text()))

    try:
        importlib.import_module("factory_config")
    except Exception as error:
        checks.append(Check("factory_config", False, str(error)))
    else:
        checks.append(Check("factory_config", True, "imports and validates"))

    env_checks = require_values(environment, (*GATEWAY_ENV, *WORKFLOW_ENV))
    checks.extend(
        Check(f"env {check.name}", check.ok, check.detail, required=require_secrets)
        for check in {check.name: check for check in env_checks}.values()
    )
    return checks


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--env-file", type=Path, default=Path(".env"), help="optional local env file (default: .env)")
    result.add_argument("--require-secrets", action="store_true", help="fail when deployment credentials are absent")
    result.add_argument("--json", action="store_true", dest="json_output")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    environment = load_environment(args.env_file)
    return print_checks(local_checks(environment, require_secrets=args.require_secrets), json_output=args.json_output)


if __name__ == "__main__":
    raise SystemExit(main())
