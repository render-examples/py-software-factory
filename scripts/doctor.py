"""Diagnose local configuration and optional read-only deployed dependencies."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import httpx
from render import Render

from scripts.common import Check, load_environment, normalize_gateway_url, print_checks
from scripts.database import database_checks
from scripts.preflight import local_checks
from scripts.setup_github import validate_or_apply
from scripts.setup_render import GATEWAY_GROUP, WORKFLOWS_GROUP, GeneratedRenderApi, RenderEnvApi

LINK_DATABASE_HINT = (
    f"run `uv run setup-render --link-database --apply`, then redeploy {WORKFLOWS_GROUP} "
    "(see docs/configuration.md#database)"
)
LOCAL_DATABASE_HINT = (
    "locally, use py-factory-db's external URL with ?sslmode=verify-full&sslrootcert=system "
    "and allow your IP on py-factory-db"
)


async def _gateway_checks(url: str) -> list[Check]:
    checks: list[Check] = []
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        for path in ("/health", "/ready"):
            try:
                response = await client.get(f"{url}{path}")
                body = response.json()
                ok = response.status_code == 200 and body.get("status") in {"ok", "ready"}
                checks.append(Check(f"gateway {path}", ok, f"HTTP {response.status_code}"))
            except Exception as error:
                checks.append(Check(f"gateway {path}", False, str(error)))
    return checks


def _render_group_check(environment: dict[str, str]) -> Check:
    api_key = environment.get("RENDER_API_KEY", "")
    workspace = environment.get("RENDER_WORKSPACE_ID", "")
    group_id = environment.get("FACTORY_SANDBOX_GROUP_ID", "")
    if not (api_key and workspace and group_id):
        return Check("sandbox group", False, "RENDER_API_KEY, RENDER_WORKSPACE_ID, or FACTORY_SANDBOX_GROUP_ID missing")
    try:
        group = Render(token=api_key, owner_id=workspace).sandboxes.get_group(group_id)
    except Exception as error:
        return Check("sandbox group", False, str(error))
    return Check("sandbox group", group.id == group_id, f"{group.name} ({group.region})")


def _database_section(environment: dict[str, str]) -> list[Check]:
    checks = database_checks(environment)
    return [
        Check(check.name, check.ok, f"{check.detail}; {LOCAL_DATABASE_HINT}", check.required)
        if check.name == "database connects" and not check.ok
        else check
        for check in checks
    ]


def _workflows_database_check(environment: dict[str, str], api: RenderEnvApi | None = None) -> Check:
    """Confirm the workflows group holds DATABASE_URL by name; its value is never read into output."""
    name = "workflows DATABASE_URL"
    try:
        if api is None:
            api = GeneratedRenderApi.from_environment(environment)
        group_id = api.env_group_id(WORKFLOWS_GROUP)
        if group_id is None:
            return Check(name, False, f"env group {WORKFLOWS_GROUP} not found; apply the Blueprint first")
        keys = api.env_group_keys(group_id)
    except Exception as error:
        return Check(name, False, str(error))
    if "DATABASE_URL" not in keys:
        return Check(name, False, f"missing from env group {WORKFLOWS_GROUP}; {LINK_DATABASE_HINT}")
    return Check(name, True, f"set in env group {WORKFLOWS_GROUP}")


def _logfire_checks(environment: dict[str, str], api: RenderEnvApi | None = None) -> list[Check]:
    """Whether each service's env group holds LOGFIRE_TOKEN, by name only. Tracing is optional, so never required."""
    try:
        if api is None:
            api = GeneratedRenderApi.from_environment(environment)
        checks: list[Check] = []
        for group in (GATEWAY_GROUP, WORKFLOWS_GROUP):
            group_id = api.env_group_id(group)
            has_token = group_id is not None and "LOGFIRE_TOKEN" in api.env_group_keys(group_id)
            detail = "set" if has_token else "not set; this service sends no traces"
            checks.append(Check(f"{group} LOGFIRE_TOKEN", has_token, detail, required=False))
        return checks
    except Exception as error:
        return [Check("LOGFIRE_TOKEN", False, str(error), required=False)]


async def _remote_checks(args: argparse.Namespace, environment: dict[str, str]) -> list[Check]:
    checks = [
        _render_group_check(environment),
        _workflows_database_check(environment),
        *_logfire_checks(environment),
    ]
    if args.gateway_url:
        try:
            gateway_url = normalize_gateway_url(args.gateway_url)
            checks.extend(await _gateway_checks(gateway_url))
        except ValueError as error:
            checks.append(Check("gateway URL", False, str(error)))
    else:
        checks.append(Check("gateway URL", False, "--gateway-url is required with --remote"))

    if args.repository:
        try:
            await validate_or_apply(args.repository, apply_labels=False)
        except Exception as error:
            checks.append(Check("GitHub App", False, str(error)))
        else:
            checks.append(Check("GitHub App", True, f"installed on {args.repository}"))
    else:
        checks.append(Check("GitHub App", False, "--repository is required with --remote"))
    return checks


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--env-file", type=Path, default=Path(".env"))
    result.add_argument("--remote", action="store_true", help="make read-only Render, GitHub, and gateway checks")
    result.add_argument("--gateway-url")
    result.add_argument("--repository", help="OWNER/NAME for the App installation check")
    result.add_argument("--json", action="store_true", dest="json_output")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    environment = load_environment(args.env_file)
    checks = local_checks(environment, require_secrets=args.remote, database=False)
    checks.extend(_database_section(environment))
    if args.remote:
        os.environ.update(environment)
        checks.extend(asyncio.run(_remote_checks(args, environment)))
    return print_checks(checks, json_output=args.json_output)


if __name__ == "__main__":
    raise SystemExit(main())
