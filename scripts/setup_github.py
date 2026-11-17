"""Validate a GitHub App installation and optionally create factory labels."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import cast
from urllib.parse import quote

from factory.config import get_config
from factory.github.auth import app_jwt, github_token
from factory.github.request import GitHubHttp
from scripts.common import fail, validate_repository


@dataclass(frozen=True)
class Label:
    name: str
    color: str
    description: str


def desired_labels() -> tuple[Label, ...]:
    labels = get_config().labels
    return (
        Label(labels.triage, "1d76db", "Ask the factory to triage this issue"),
        Label(labels.spec_approved, "0e8a16", "Approve the current specification for implementation"),
        Label(labels.pr_review, "8250df", "Ask the factory to review this draft pull request"),
        Label(labels.autopilot, "fbca04", "Run triage, implementation, and review"),
    )


async def installation_id(repository: str) -> int:
    async def token() -> str:
        return app_jwt()

    http = GitHubHttp(token)
    try:
        raw: object = await http.json("GET", f"/repos/{repository}/installation")
    finally:
        await http.aclose()
    value = cast(dict[str, object], raw).get("id") if isinstance(raw, dict) else None
    if not isinstance(value, int):
        raise RuntimeError("GitHub returned no installation ID")
    return value


async def validate_or_apply(repository: str, *, apply_labels: bool) -> int:
    owner, name = repository.split("/", 1)
    app_http = GitHubHttp(lambda: _app_token())
    try:
        app: object = await app_http.json("GET", "/app")
    finally:
        await app_http.aclose()
    install = await installation_id(repository)
    token = await github_token(install, name)

    async def installation_token() -> str:
        return token

    http = GitHubHttp(installation_token)
    try:
        await http.json("GET", f"/repos/{owner}/{name}")
        if apply_labels:
            for label in desired_labels():
                await _ensure_label(http, owner, name, label)
    finally:
        await http.aclose()
    slug_value = cast(dict[str, object], app).get("slug") if isinstance(app, dict) else None
    slug = slug_value if isinstance(slug_value, str) else "<unknown>"
    print(f"Validated GitHub App {slug!r}, installation {install}, repository {repository}.")
    if apply_labels:
        print(f"Ensured {len(desired_labels())} labels.")
    return install


async def _app_token() -> str:
    return app_jwt()


async def _ensure_label(http: GitHubHttp, owner: str, repo: str, label: Label) -> None:
    path = f"/repos/{owner}/{repo}/labels"
    existing = cast(list[object], await http.paginate(path))
    by_name = {
        name
        for item in existing
        if isinstance(item, dict)
        for name in [cast(dict[str, object], item).get("name")]
        if isinstance(name, str)
    }
    body = {"name": label.name, "color": label.color, "description": label.description}
    if label.name in by_name:
        await http.json("PATCH", f"{path}/{quote(label.name, safe='')}", body=body, idempotent=True)
    else:
        await http.json("POST", path, body=body)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("repository", help="configured OWNER/NAME")
    result.add_argument("--check", action="store_true", help="perform read-only App and installation API checks")
    result.add_argument("--apply-labels", action="store_true", help="create or update labels (mutates GitHub)")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        repository = validate_repository(args.repository)
    except ValueError as error:
        return fail(str(error))
    print("Required App events: issues, issue_comment, pull_request, push.")
    print("Required permissions: contents:write, issues:write, members:read, metadata:read, pull_requests:write.")
    for label in desired_labels():
        print(f"- {label.name}: {label.description}")
    if not (args.check or args.apply_labels):
        print("Dry run only. Use --check for read-only API validation or --apply-labels to mutate labels.")
        return 0
    try:
        asyncio.run(validate_or_apply(repository, apply_labels=args.apply_labels))
    except Exception as error:
        return fail(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
