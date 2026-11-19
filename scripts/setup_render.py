"""Plan or create the factory sandbox group and link the database; secrets remain Dashboard-managed."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Protocol, cast

from render import Render
from render.public_api.api.environment_groups import (
    list_env_groups,
    retrieve_env_group,
    update_env_group_env_var,
)
from render.public_api.api.services import list_services, retrieve_env_var
from render.public_api.client import AuthenticatedClient
from render.public_api.models import EnvGroup, EnvVar, EnvVarValue, Error, ServiceType
from render.public_api.types import UNSET, Unset

from factory.sandbox.credentials import ANTHROPIC_CREDENTIAL, credential_matches, ensure_credential
from scripts.common import SANDBOX_GROUP_RE, fail, parse_env_file, write_private

GROUP_NAME = "factory-sandboxes"
REGION = "oregon"
GATEWAY_NAME_RE = re.compile(r"^factory-gateway(-[a-z0-9]+)?$")
WORKFLOWS_GROUP = "factory-workflows"
EVENTS_READ_TOKEN = "EVENTS_READ_TOKEN"  # noqa: S105 - the variable name, not a value
PAGE_SIZE = 100


def render_handoff(group_id: str | None = None) -> list[str]:
    group = group_id or "<sbg-id>"
    return [
        "Blueprint env groups: factory-gateway and factory-workflows (created by render.yaml).",
        (
            "factory-gateway: RENDER_API_KEY, GITHUB_WEBHOOK_SECRET, "
            "EVENTS_READ_TOKEN (generated into .env by setup-render)."
        ),
        (
            "factory-workflows: RENDER_API_KEY, RENDER_WORKSPACE_ID, GITHUB_APP_ID, "
            "GITHUB_APP_PRIVATE_KEY, ANTHROPIC_API_KEY, FACTORY_SANDBOX_GROUP_ID."
        ),
        f"Set FACTORY_SANDBOX_GROUP_ID={group} only on factory-workflows.",
        (
            "DATABASE_URL: the Blueprint links factory-db to factory-gateway. After applying it, run "
            "`setup-render --link-database --apply` to copy it into factory-workflows, then redeploy factory-workflows."
        ),
        (
            "Optional credential broker for in-sandbox loops: api.anthropic.com, inject x-api-key, restrict to "
            "POST /v1/messages. Use --apply-credential only after enabling brokered placement."
        ),
        "Never put RENDER_ADMIN_API_KEY in either runtime environment group.",
    ]


def find_group(render: Render, name: str) -> str | None:
    cursor: str | None = None
    while True:
        page = render.sandboxes.list_groups(cursor=cursor, limit=100)
        for group in page.groups:
            if group.name == name:
                return group.id
        cursor = page.next_cursor
        if not cursor:
            return None


def ensure_group(environment: Mapping[str, str], name: str, *, apply: bool) -> tuple[str | None, bool]:
    api_key = environment.get("RENDER_ADMIN_API_KEY") or environment.get("RENDER_API_KEY")
    workspace = environment.get("RENDER_WORKSPACE_ID")
    if not api_key or not workspace:
        raise ValueError("RENDER_API_KEY (or RENDER_ADMIN_API_KEY) and RENDER_WORKSPACE_ID are required")
    render = Render(token=api_key, owner_id=workspace, region=REGION)
    existing = find_group(render, name)
    if existing:
        return existing, False
    if not apply:
        return None, False
    created = render.sandboxes.create_group(name, region=REGION)
    return created.id, True


@dataclass(frozen=True)
class ServiceRef:
    id: str
    name: str


class RenderEnvApi(Protocol):
    """The Render calls that move DATABASE_URL between a service and an env group."""

    def web_services(self) -> list[ServiceRef]: ...

    def service_env_var(self, service_id: str, key: str) -> str | None: ...

    def env_group_id(self, name: str) -> str | None: ...

    def env_group_keys(self, group_id: str) -> set[str]: ...

    def set_env_group_var(self, group_id: str, key: str, value: str) -> None: ...


class GeneratedRenderApi:
    """`RenderEnvApi` over the SDK's generated client. Values it reads must never be formatted into messages."""

    def __init__(self, token: str, workspace: str) -> None:
        self._client = cast(AuthenticatedClient, Render(token=token, owner_id=workspace).client.internal)
        self._workspace = workspace

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> GeneratedRenderApi:
        token = (environment.get("RENDER_ADMIN_API_KEY") or environment.get("RENDER_API_KEY") or "").strip()
        workspace = environment.get("RENDER_WORKSPACE_ID", "").strip()
        if not token or not workspace:
            raise ValueError("RENDER_API_KEY (or RENDER_ADMIN_API_KEY) and RENDER_WORKSPACE_ID are required")
        return cls(token, workspace)

    def web_services(self) -> list[ServiceRef]:
        services: list[ServiceRef] = []
        cursor: str | Unset = UNSET
        while True:
            page = list_services.sync(
                client=self._client,
                type_=[ServiceType.WEB_SERVICE],
                owner_id=[self._workspace],
                include_previews=False,
                cursor=cursor,
                limit=PAGE_SIZE,
            )
            if not isinstance(page, list):
                raise RuntimeError(f"could not list services: {_error_text(page)}")
            services.extend(ServiceRef(item.service.id, item.service.name) for item in page)
            if len(page) < PAGE_SIZE:
                return services
            cursor = page[-1].cursor

    def service_env_var(self, service_id: str, key: str) -> str | None:
        response = retrieve_env_var.sync_detailed(service_id, key, client=self._client)
        if response.status_code == HTTPStatus.NOT_FOUND:
            return None
        if isinstance(response.parsed, EnvVar):
            return response.parsed.value
        raise RuntimeError(f"could not read {key} on {service_id}: HTTP {int(response.status_code)}")

    def env_group_id(self, name: str) -> str | None:
        groups = list_env_groups.sync(client=self._client, name=[name], owner_id=[self._workspace], limit=PAGE_SIZE)
        if not isinstance(groups, list):
            raise RuntimeError(f"could not list env groups: {_error_text(groups)}")
        matches = [group.id for group in groups if group.name == name]
        if len(matches) > 1:
            raise RuntimeError(f"several env groups are named {name}: {', '.join(matches)}")
        return matches[0] if matches else None

    def env_group_keys(self, group_id: str) -> set[str]:
        group = retrieve_env_group.sync(group_id, client=self._client)
        if not isinstance(group, EnvGroup):
            raise RuntimeError(f"could not read env group {group_id}: {_error_text(group)}")
        return {env_var.key for env_var in group.env_vars}

    def set_env_group_var(self, group_id: str, key: str, value: str) -> None:
        result = update_env_group_env_var.sync(group_id, key, client=self._client, body=EnvVarValue(value=value))
        if not isinstance(result, EnvGroup):
            raise RuntimeError(f"could not write {key} to env group {group_id}: {_error_text(result)}")


def _error_text(result: object) -> str:
    if isinstance(result, Error) and isinstance(result.message, str):
        return result.message
    return "unexpected response"


def link_database(
    environment: Mapping[str, str],
    *,
    apply: bool,
    api: RenderEnvApi | None = None,
) -> list[str]:
    """Copy the gateway's Blueprint-provided DATABASE_URL into the factory-workflows group, without echoing it."""
    if api is None:
        api = GeneratedRenderApi.from_environment(environment)
    gateways = [service for service in api.web_services() if GATEWAY_NAME_RE.fullmatch(service.name)]
    if not gateways:
        raise ValueError("no factory-gateway web service in this workspace; apply the Blueprint first")
    if len(gateways) > 1:
        names = ", ".join(f"{service.name} ({service.id})" for service in gateways)
        raise ValueError(f"several gateway services match: {names}; expected exactly one")
    [gateway] = gateways
    value = api.service_env_var(gateway.id, "DATABASE_URL")
    if not value:
        raise ValueError(f"{gateway.name} has no DATABASE_URL; apply the Blueprint first")
    group_id = api.env_group_id(WORKFLOWS_GROUP)
    if group_id is None:
        raise ValueError(f"env group {WORKFLOWS_GROUP} not found; apply the Blueprint first")
    target = f"DATABASE_URL from {gateway.name} ({gateway.id}) to env group {WORKFLOWS_GROUP} ({group_id})"
    if not apply:
        return [f"Dry run: would copy {target}. Rerun with --apply to write it; the value is never printed."]
    api.set_env_group_var(group_id, "DATABASE_URL", value)
    return [f"Copied {target}.", f"Redeploy {WORKFLOWS_GROUP} so the running service picks up DATABASE_URL."]


def ensure_events_read_token(path: Path) -> bool:
    """Write a generated EVENTS_READ_TOKEN to an env file that has none. The token is never printed."""
    text = path.read_text() if path.exists() else ""
    if parse_env_file(text).get(EVENTS_READ_TOKEN, "").strip():
        return False
    line = f"{EVENTS_READ_TOKEN}={secrets.token_hex(32)}"
    empty = re.compile(rf"^[ \t]*(?:export[ \t]+)?{EVENTS_READ_TOKEN}[ \t]*=.*$", re.MULTILINE)
    if empty.search(text):
        text = empty.sub(lambda _: line, text)
    else:
        separator = "" if not text or text.endswith("\n") else "\n"
        text = f"{text}{separator}{line}\n"
    write_private(path, text)
    return True


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--group-name", default=GROUP_NAME)
    result.add_argument("--apply-group", action="store_true", help="create the group if absent (mutates Render)")
    result.add_argument(
        "--apply-credential",
        action="store_true",
        help="create or rotate the restricted Anthropic credential (mutates Render)",
    )
    result.add_argument("--group-id", help="skip API access and print handoff for this existing group")
    result.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="local env file that receives a generated EVENTS_READ_TOKEN if it has none (default: .env)",
    )
    result.add_argument(
        "--link-database",
        action="store_true",
        help="copy the gateway's DATABASE_URL into the factory-workflows group (dry run without --apply)",
    )
    result.add_argument("--apply", action="store_true", help="with --link-database, write the value (mutates Render)")
    return result


def main(argv: list[str] | None = None, *, api: RenderEnvApi | None = None) -> int:
    args = parser().parse_args(argv)
    if args.apply and not args.link_database:
        return fail("--apply is only used with --link-database")
    if args.link_database:
        try:
            lines = link_database(os.environ, apply=args.apply, api=api)
        except Exception as error:
            return fail(str(error))
        for line in lines:
            print(line)
        return 0
    try:
        generated = ensure_events_read_token(args.env_file)
    except (OSError, ValueError) as error:
        return fail(f"could not update {args.env_file}: {error}")
    if generated:
        print(
            f"Generated {EVENTS_READ_TOKEN} in {args.env_file} (value not shown). "
            "Add the same value to the factory-gateway env group."
        )
    else:
        print(f"{EVENTS_READ_TOKEN} is set in {args.env_file}; the factory-gateway env group must hold the same value.")
    if args.group_id:
        if not SANDBOX_GROUP_RE.fullmatch(args.group_id):
            return fail("--group-id must look like sbg-...")
        group_id, created = args.group_id, False
    else:
        try:
            group_id, created = ensure_group(os.environ, args.group_name, apply=args.apply_group)
        except Exception as error:
            if args.apply_group:
                return fail(str(error))
            print(f"Render lookup unavailable: {error}")
            group_id, created = None, False
    if created:
        print(f"Created sandbox group {args.group_name} ({group_id}).")
    elif group_id:
        print(f"Using sandbox group {args.group_name} ({group_id}).")
    else:
        print(f"Dry run: sandbox group {args.group_name!r} would be created in {REGION}.")
    if args.apply_credential:
        if group_id is None:
            return fail("--apply-credential needs an existing group or --apply-group")
        secret = os.environ.get("ANTHROPIC_API_KEY", "")
        if not secret:
            return fail("ANTHROPIC_API_KEY is required with --apply-credential")
        try:
            configured = asyncio.run(ensure_credential(group_id, ANTHROPIC_CREDENTIAL, secret))
        except Exception as error:
            return fail(str(error))
        if not credential_matches(configured, ANTHROPIC_CREDENTIAL):
            return fail("Render returned credential metadata that does not match the requested restriction")
        print(
            f"Configured {configured.id}: {configured.domain}, "
            f"{','.join(configured.methods)} {','.join(configured.paths)}."
        )
    for step in render_handoff(group_id):
        print(f"- {step}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
