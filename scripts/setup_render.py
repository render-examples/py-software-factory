"""Plan or create the factory sandbox group, fill the services' env groups from env files, and link the database."""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, Protocol, cast

from render import Render
from render.public_api.api.environment_groups import (
    create_env_group,
    retrieve_env_group,
    update_env_group_env_var,
)
from render.public_api.api.services import list_services, retrieve_env_var
from render.public_api.client import AuthenticatedClient
from render.public_api.models import (
    EnvGroup,
    EnvGroupPOSTInput,
    EnvVar,
    EnvVarKeyValue,
    EnvVarValue,
    Error,
    ServiceType,
)
from render.public_api.types import UNSET, Unset

from scripts.common import SANDBOX_GROUP_RE, fail, load_environment, parse_env_file

GROUP_NAME = "py-factory-sandboxes"
REGION = "oregon"
GATEWAY_NAME_RE = re.compile(r"^py-factory-gateway(-[a-z0-9]+)?$")
GATEWAY_GROUP = "py-factory-gateway"
WORKFLOWS_GROUP = "py-factory-workflows"
PAGE_SIZE = 100
DEFAULT_ENV_FILE = Path(".env")
GROUP_KEYS: Mapping[str, tuple[str, ...]] = {
    GATEWAY_GROUP: ("RENDER_API_KEY", "GITHUB_WEBHOOK_SECRET", "LOGFIRE_ENVIRONMENT"),
    WORKFLOWS_GROUP: (
        "RENDER_API_KEY",
        "RENDER_WORKSPACE_ID",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "ANTHROPIC_API_KEY",
        "FACTORY_SANDBOX_GROUP_ID",
        "GITHUB_API_URL",
        "LOGFIRE_ENVIRONMENT",
        "LOGFIRE_PROJECT_URL",
    ),
}


def render_handoff(group_id: str | None = None) -> list[str]:
    group = group_id or "<sbg-id>"
    return [
        (
            "Env groups: py-factory-gateway and py-factory-workflows. Create and fill them from .env with "
            "`setup-render --env-groups --apply` before applying the Blueprint, which only links them."
        ),
        "py-factory-gateway: RENDER_API_KEY, GITHUB_WEBHOOK_SECRET.",
        (
            "py-factory-workflows: RENDER_API_KEY, RENDER_WORKSPACE_ID, GITHUB_APP_ID, "
            "GITHUB_APP_PRIVATE_KEY, ANTHROPIC_API_KEY, FACTORY_SANDBOX_GROUP_ID."
        ),
        f"Set FACTORY_SANDBOX_GROUP_ID={group} only on py-factory-workflows.",
        (
            "DATABASE_URL: the Blueprint links py-factory-db to py-factory-gateway. After applying it, run "
            "`setup-render --link-database --apply` to copy it into py-factory-workflows, then redeploy "
            "py-factory-workflows."
        ),
        (
            "Optional tracing: add a Logfire write token as LOGFIRE_TOKEN to each group, a separate token per "
            "service so either can be revoked alone."
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
    def web_services(self) -> list[ServiceRef]: ...

    def service_env_var(self, service_id: str, key: str) -> str | None: ...

    def env_group_id(self, name: str) -> str | None: ...

    def env_group_keys(self, group_id: str) -> set[str]: ...

    def set_env_group_var(self, group_id: str, key: str, value: str) -> None: ...

    def create_env_group(self, name: str, values: Mapping[str, str]) -> str: ...


class GeneratedRenderApi:
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
        response = self._client.get_httpx_client().get(
            "/env-groups", params={"name": name, "ownerId": self._workspace, "limit": PAGE_SIZE}
        )
        if response.status_code != HTTPStatus.OK:
            raise RuntimeError(f"could not list env groups: HTTP {response.status_code}")
        matches = [group["id"] for group in env_groups(response.json()) if group.get("name") == name]
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

    def create_env_group(self, name: str, values: Mapping[str, str]) -> str:
        body = EnvGroupPOSTInput(
            name=name,
            owner_id=self._workspace,
            env_vars=[EnvVarKeyValue(key=key, value=value) for key, value in values.items()],
        )
        result = create_env_group.sync(client=self._client, body=body)
        if not isinstance(result, EnvGroup):
            raise RuntimeError(f"could not create env group {name}: {_error_text(result)}")
        return result.id


def env_groups(body: object) -> list[dict[str, Any]]:
    if not isinstance(body, list):
        raise RuntimeError("env group list response is not a list")
    groups: list[dict[str, Any]] = []
    for item in cast(list[object], body):
        if not isinstance(item, dict):
            continue
        record = cast(dict[str, Any], item)
        group = record.get("envGroup", record)
        if isinstance(group, dict) and isinstance(cast(dict[str, Any], group).get("id"), str):
            groups.append(cast(dict[str, Any], group))
    return groups


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
    if api is None:
        api = GeneratedRenderApi.from_environment(environment)
    gateways = [service for service in api.web_services() if GATEWAY_NAME_RE.fullmatch(service.name)]
    if not gateways:
        raise ValueError("no py-factory-gateway web service in this workspace; apply the Blueprint first")
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


def read_env_files(paths: Sequence[Path]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        if not path.is_file():
            raise ValueError(f"env file {path} not found")
        values.update(parse_env_file(path.read_text()))
    return values


def sync_env_groups(values: Mapping[str, str], *, apply: bool, api: RenderEnvApi) -> list[str]:
    lines: list[str] = []
    for group, keys in GROUP_KEYS.items():
        present = {key: values[key].strip() for key in keys if values.get(key, "").strip()}
        names = ", ".join(present) or "no keys"
        group_id = api.env_group_id(group)
        if group_id is None:
            if apply:
                group_id = api.create_env_group(group, present)
                lines.append(f"Created env group {group} ({group_id}) with {names}.")
            else:
                lines.append(f"Dry run: would create env group {group} with {names}.")
        elif not present:
            lines.append(f"Env group {group} ({group_id}): nothing to set.")
        elif apply:
            for key, value in present.items():
                api.set_env_group_var(group_id, key, value)
            lines.append(f"Set {names} in env group {group} ({group_id}).")
        else:
            lines.append(f"Dry run: would set {names} in env group {group} ({group_id}).")
        missing = [key for key in keys if key not in present]
        if missing:
            lines.append(f"  Not in the env files: {', '.join(missing)}.")
    lines.append("LOGFIRE_TOKEN is never copied: add a separate write token to each group in the Dashboard.")
    if not apply:
        lines.append("Rerun with --apply to write these; values are never printed.")
    else:
        lines.append("Redeploy any running service whose group changed.")
    return lines


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--group-name", default=GROUP_NAME)
    result.add_argument("--apply-group", action="store_true", help="create the group if absent (mutates Render)")
    result.add_argument("--group-id", help="skip API access and print handoff for this existing group")
    result.add_argument(
        "--env-groups",
        action="store_true",
        help="create the py-factory-gateway and py-factory-workflows env groups and fill them from env files "
        "(dry run without --apply)",
    )
    result.add_argument(
        "--env-file",
        type=Path,
        action="append",
        help="with --env-groups, an env file to read values from; repeatable (default: .env)",
    )
    result.add_argument(
        "--link-database",
        action="store_true",
        help="copy the gateway's DATABASE_URL into the py-factory-workflows group (dry run without --apply)",
    )
    result.add_argument(
        "--apply",
        action="store_true",
        help="with --env-groups or --link-database, write the values (mutates Render)",
    )
    return result


def main(argv: list[str] | None = None, *, api: RenderEnvApi | None = None) -> int:
    args = parser().parse_args(argv)
    if args.env_groups and args.link_database:
        return fail("use --env-groups and --link-database separately")
    if args.apply and not (args.env_groups or args.link_database):
        return fail("--apply is only used with --env-groups or --link-database")
    if args.env_file and not args.env_groups:
        return fail("--env-file is only used with --env-groups")
    if args.env_groups:
        try:
            values = read_env_files(args.env_file or [DEFAULT_ENV_FILE])
            if api is None:
                api = GeneratedRenderApi.from_environment(load_environment(DEFAULT_ENV_FILE))
            lines = sync_env_groups(values, apply=args.apply, api=api)
        except Exception as error:
            return fail(str(error))
        for line in lines:
            print(line)
        return 0
    if args.link_database:
        try:
            lines = link_database(os.environ, apply=args.apply, api=api)
        except Exception as error:
            return fail(str(error))
        for line in lines:
            print(line)
        return 0
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
    for step in render_handoff(group_id):
        print(f"- {step}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
