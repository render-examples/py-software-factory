"""Diagnose local configuration and optional read-only deployed dependencies."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import httpx
from render import Render

from factory.sandbox.credentials import ANTHROPIC_CREDENTIAL, credential_matches, list_credentials
from scripts.common import Check, load_environment, normalize_gateway_url, print_checks
from scripts.preflight import local_checks
from scripts.setup_github import validate_or_apply


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


def _credential_check(environment: dict[str, str]) -> Check:
    group_id = environment.get("FACTORY_SANDBOX_GROUP_ID", "")
    api_key = environment.get("RENDER_ADMIN_API_KEY") or environment.get("RENDER_API_KEY")
    if not group_id or not api_key:
        return Check("credential brokering", False, "sandbox group or Render API key missing")
    try:
        credentials = asyncio.run(list_credentials(group_id, admin_api_key=api_key))
    except Exception as error:
        return Check("credential brokering", False, str(error))
    actual = next((item for item in credentials if item.id == ANTHROPIC_CREDENTIAL.id), None)
    if actual is None:
        return Check("credential brokering", False, f"{ANTHROPIC_CREDENTIAL.id} is missing")
    return Check(
        "credential brokering",
        credential_matches(actual, ANTHROPIC_CREDENTIAL),
        f"{actual.domain}; methods={actual.methods}; paths={actual.paths}",
    )


async def _broker_probe(repository: str, installation_id: int) -> Check:
    """Prove header injection with one explicit, minimal model request."""
    from factory.agents.providers import provider_for_model
    from factory.agents.registry import TRIAGE
    from factory.config import require_repo
    from factory.sandbox import BranchRef, open_sandbox, shq

    repo = require_repo(repository, installation_id)
    provider = provider_for_model(TRIAGE.model)
    model_name = TRIAGE.model.partition(":")[2]
    program = "\n".join(
        [
            "import httpx2,sys",
            f"url='https://{provider.api_host}/v1/messages'",
            "response=httpx2.post(url,headers={'anthropic-version':'2023-06-01'},",
            f" json={{'model':{model_name!r},'max_tokens':1,'messages':[{{'role':'user','content':'Reply OK'}}]}},",
            " timeout=30)",
            "print(response.status_code)",
            "print(response.text[:1000])",
            "sys.exit(0 if response.status_code == 200 else 1)",
        ]
    )
    sandbox = None
    try:
        sandbox = await open_sandbox(
            repo,
            BranchRef(repo.config.base_branch),
            model_egress=(provider.api_host,),
        )
        result = await sandbox.exec(
            f"/opt/factory/venv/bin/python -c {shq(program)}",
            timeout_seconds=60,
            as_runner=True,
        )
        detail = (result.stdout or result.stderr).strip()[-1000:]
        return Check("credential broker live probe", result.exit_code == 0, detail)
    except Exception as error:
        return Check("credential broker live probe", False, str(error))
    finally:
        if sandbox is not None:
            await sandbox.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--env-file", type=Path, default=Path(".env"))
    result.add_argument("--remote", action="store_true", help="make read-only Render, GitHub, and gateway checks")
    result.add_argument("--gateway-url")
    result.add_argument("--repository", help="OWNER/NAME for the App installation check")
    result.add_argument(
        "--probe-brokering",
        action="store_true",
        help="open a disposable sandbox and make one minimal billable model request",
    )
    result.add_argument("--json", action="store_true", dest="json_output")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.probe_brokering and not args.remote:
        return print_checks(
            [Check("credential broker live probe", False, "--probe-brokering requires --remote")],
            json_output=args.json_output,
        )
    environment = load_environment(args.env_file)
    checks = local_checks(environment, require_secrets=args.remote)
    if args.remote:
        os.environ.update(environment)
        checks.append(_render_group_check(environment))
        checks.append(_credential_check(environment))
        if args.gateway_url:
            try:
                gateway_url = normalize_gateway_url(args.gateway_url)
                checks.extend(asyncio.run(_gateway_checks(gateway_url)))
            except ValueError as error:
                checks.append(Check("gateway URL", False, str(error)))
        else:
            checks.append(Check("gateway URL", False, "--gateway-url is required with --remote"))
        installation: int | None = None
        if args.repository:
            try:
                installation = asyncio.run(validate_or_apply(args.repository, apply_labels=False))
            except Exception as error:
                checks.append(Check("GitHub App", False, str(error)))
            else:
                checks.append(Check("GitHub App", True, f"installed on {args.repository}"))
        else:
            checks.append(Check("GitHub App", False, "--repository is required with --remote"))
        if args.probe_brokering:
            if not args.repository or installation is None:
                checks.append(Check("credential broker live probe", False, "GitHub App validation must pass first"))
            else:
                checks.append(asyncio.run(_broker_probe(args.repository, installation)))
    return print_checks(checks, json_output=args.json_output)


if __name__ == "__main__":
    raise SystemExit(main())
