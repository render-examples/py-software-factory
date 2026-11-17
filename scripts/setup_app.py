"""Build and validate the GitHub App manifest used by the factory."""

from __future__ import annotations

import argparse
import html
import json
import secrets
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import httpx

from scripts.common import fail, normalize_gateway_url, write_private

EVENTS = ("issues", "issue_comment", "pull_request", "push")
PERMISSIONS = {
    "contents": "write",
    "issues": "write",
    "members": "read",
    "metadata": "read",
    "pull_requests": "write",
}


def app_manifest(name: str, gateway_url: str, *, description: str = "") -> dict[str, Any]:
    base = normalize_gateway_url(gateway_url)
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("app name must not be empty")
    manifest: dict[str, Any] = {
        "name": clean_name,
        "url": base,
        "hook_attributes": {"url": f"{base}/webhooks/github", "active": True},
        "redirect_url": f"{base}/",
        "public": False,
        "default_permissions": PERMISSIONS,
        "default_events": list(EVENTS),
    }
    if description.strip():
        manifest["description"] = description.strip()
    return manifest


def manifest_form(manifest: dict[str, Any], *, state: str, organization: str | None = None) -> str:
    action = (
        f"https://github.com/organizations/{organization}/settings/apps/new"
        if organization
        else "https://github.com/settings/apps/new"
    )
    encoded = html.escape(json.dumps(manifest), quote=True)
    return (
        '<!doctype html><meta charset="utf-8"><title>Create factory GitHub App</title>'
        f'<form action="{action}?state={html.escape(state, quote=True)}" method="post">'
        f'<input type="hidden" name="manifest" value="{encoded}">'
        '<button type="submit">Create factory GitHub App</button></form>'
    )


def exchange_manifest_code(code: str) -> dict[str, Any]:
    response = httpx.post(
        f"https://api.github.com/app-manifests/{quote(code, safe='')}/conversions",
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        timeout=15,
    )
    if not response.is_success:
        raise RuntimeError(f"GitHub manifest conversion failed ({response.status_code}): {response.text[:200]}")
    result = cast(dict[str, Any], response.json())
    if not all(result.get(key) for key in ("id", "pem", "webhook_secret")):
        raise RuntimeError("GitHub manifest conversion omitted id, pem, or webhook_secret")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--name", default="py-software-factory")
    result.add_argument("--gateway-url", help="deployed gateway URL, for example https://factory.onrender.com")
    result.add_argument("--description", default="GitHub-native software factory on Render")
    result.add_argument("--output", type=Path, help="write manifest JSON with mode 0600 instead of stdout")
    result.add_argument("--html-output", type=Path, help="write a local POST form for the GitHub manifest flow")
    result.add_argument("--organization", help="create the App for this organization instead of the current user")
    result.add_argument("--exchange-code", help="exchange GitHub's one-time manifest code")
    result.add_argument("--credentials-output", type=Path, help="required private output for exchanged credentials")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.exchange_code:
        if not args.credentials_output:
            return fail("--credentials-output is required with --exchange-code")
        try:
            result = exchange_manifest_code(args.exchange_code)
        except RuntimeError as error:
            return fail(str(error))
        private_key = str(result["pem"]).replace("\n", "\\n")
        credentials = (
            f"GITHUB_APP_ID={result['id']}\n"
            f"GITHUB_APP_PRIVATE_KEY={private_key}\n"
            f"GITHUB_WEBHOOK_SECRET={result['webhook_secret']}\n"
        )
        write_private(args.credentials_output, credentials)
        print(f"Wrote one-time App credentials to {args.credentials_output} with owner-only permissions.")
        return 0
    if not args.gateway_url:
        return fail("--gateway-url is required when generating a manifest")
    try:
        manifest = app_manifest(args.name, args.gateway_url, description=args.description)
    except ValueError as error:
        return fail(str(error))
    content = json.dumps(manifest, indent=2) + "\n"
    if args.output:
        write_private(args.output, content)
        print(f"Wrote {args.output} with owner-only permissions.")
    else:
        print(content, end="")
    state = secrets.token_urlsafe(18)
    if args.html_output:
        write_private(args.html_output, manifest_form(manifest, state=state, organization=args.organization))
        print(f"Wrote submit form to {args.html_output}. Open it in a browser and verify the settings before submit.")
    print(
        "\nNext: submit the manifest using GitHub's required POST flow. Use --html-output to generate the form. "
        "After redirect, copy the `code` query value and run `setup-app --exchange-code CODE "
        "--credentials-output tmp/github-app.env`. See docs/runbooks/setup.md."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
