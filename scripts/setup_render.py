"""Plan or create the factory sandbox group; secrets remain Dashboard-managed."""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Mapping

from render import Render

from factory.sandbox.credentials import ANTHROPIC_CREDENTIAL, credential_matches, ensure_credential
from scripts.common import SANDBOX_GROUP_RE, fail

GROUP_NAME = "factory-sandboxes"
REGION = "oregon"


def render_handoff(group_id: str | None = None) -> list[str]:
    group = group_id or "<sbg-id>"
    return [
        "Blueprint env groups: factory-gateway and factory-workflows (created by render.yaml).",
        "factory-gateway: RENDER_API_KEY, GITHUB_WEBHOOK_SECRET.",
        (
            "factory-workflows: RENDER_API_KEY, RENDER_WORKSPACE_ID, GITHUB_APP_ID, "
            "GITHUB_APP_PRIVATE_KEY, ANTHROPIC_API_KEY, FACTORY_SANDBOX_GROUP_ID."
        ),
        f"Set FACTORY_SANDBOX_GROUP_ID={group} only on factory-workflows.",
        (
            "Credential broker: api.anthropic.com, inject x-api-key, restrict to POST /v1/messages. "
            "Use --apply-credential to create or rotate it with Render's generated API."
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
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
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
