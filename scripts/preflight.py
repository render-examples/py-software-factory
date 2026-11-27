"""Local deployment preflight. This command never calls Render or GitHub; it connects to Postgres only when
DATABASE_URL is set."""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import cast

import yaml

from scripts.common import ROOT, Check, command_check, load_environment, print_checks, require_values
from scripts.database import database_checks

GATEWAY_ENV = ("RENDER_API_KEY", "GITHUB_WEBHOOK_SECRET", "DATABASE_URL")
WORKFLOW_ENV = (
    "RENDER_API_KEY",
    "RENDER_WORKSPACE_ID",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "ANTHROPIC_API_KEY",
    "FACTORY_SANDBOX_GROUP_ID",
    "DATABASE_URL",
)
DATABASE_NAME = "py-factory-db"
FREE_PLANS = {"free"}


def blueprint_checks(text: str) -> list[Check]:
    try:
        parsed: object = yaml.safe_load(text)
    except yaml.YAMLError as error:
        return [Check("blueprint YAML", False, str(error))]
    if not isinstance(parsed, dict):
        return [Check("blueprint YAML", False, "top-level value must be a mapping")]
    document = cast(dict[str, object], parsed)
    raw_services = document.get("services")
    service_values = cast(list[object], raw_services) if isinstance(raw_services, list) else []
    services = [cast(dict[str, object], service) for service in service_values if isinstance(service, dict)]
    service_types = [service.get("type") for service in services]
    empty: dict[str, object] = {}
    gateway = next((service for service in services if service.get("type") == "web"), empty)
    workflow = next((service for service in services if service.get("type") == "workflow"), empty)
    raw_databases = document.get("databases")
    database_values = cast(list[object], raw_databases) if isinstance(raw_databases, list) else []
    database = next(
        (
            cast(dict[str, object], item)
            for item in database_values
            if isinstance(item, dict) and cast(dict[str, object], item).get("name") == DATABASE_NAME
        ),
        None,
    )
    gateway_link = any(
        entry.get("key") == "DATABASE_URL"
        and entry.get("fromDatabase") == {"name": DATABASE_NAME, "property": "connectionString"}
        for entry in _env_entries(gateway)
    )
    workflow_references_database = any(
        "fromDatabase" in entry or entry.get("key") == "DATABASE_URL" for entry in _env_entries(workflow)
    )
    plan = database.get("plan") if database else None
    env_groups = _environment_groups(services)
    return [
        Check("blueprint services", service_types == ["web", "workflow"], repr(service_types)),
        Check(
            "blueprint database",
            database is not None and gateway_link,
            f"declares {DATABASE_NAME} and links its connectionString to the gateway's DATABASE_URL",
        ),
        Check(
            "database plan",
            isinstance(plan, str) and plan not in FREE_PLANS,
            f"explicit paid plan ({plan!r}); free Postgres expires after 30 days",
        ),
        Check(
            "database access",
            database is not None and database.get("ipAllowList") == [],
            "ipAllowList: [] blocks connections from outside the workspace",
        ),
        Check(
            "workflow database",
            not workflow_references_database,
            "no database reference (Blueprint sync rejects it); DATABASE_URL comes from the py-factory-workflows group",
        ),
        Check(
            "workflow slug",
            any(
                entry.get("key") == "RENDER_WORKFLOW_SLUG" and entry.get("value") == "py-factory-workflows"
                for entry in _env_entries(workflow)
            ),
            "RENDER_WORKFLOW_SLUG=py-factory-workflows",
        ),
        Check("workflow runtime", workflow.get("runtime") == "python", "Python runtime"),
        Check("workflow build", workflow.get("buildCommand") == "uv sync --frozen --no-dev", "locked uv build"),
        Check("workflow plan", "plan" not in workflow, "service-level plan omitted"),
        Check(
            "secret groups",
            env_groups == {"py-factory-gateway", "py-factory-workflows"} and "envVarGroups" not in document,
            "gateway and workflow groups linked, not declared; `setup-render --env-groups` creates them",
        ),
    ]


def _env_entries(service: dict[str, object]) -> list[dict[str, object]]:
    env_vars = service.get("envVars")
    if not isinstance(env_vars, list):
        return []
    return [cast(dict[str, object], entry) for entry in cast(list[object], env_vars) if isinstance(entry, dict)]


def _environment_groups(services: list[dict[str, object]]) -> set[str]:
    groups: set[str] = set()
    for service in services:
        for entry in _env_entries(service):
            group = entry.get("fromGroup")
            if isinstance(group, str):
                groups.add(group)
    return groups


def local_checks(
    environment: dict[str, str],
    *,
    require_secrets: bool = False,
    database: bool = True,
) -> list[Check]:
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
    if database:
        checks.extend(database_checks(environment))
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
