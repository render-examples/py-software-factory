"""Spec drafting, validation, and publication."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Literal, Protocol, TypedDict

from factory.claims import ExpectedFailure, Outcome, StatusLink
from factory.config import get_config
from factory.format import render_spec, sanitize_inline, truncate
from factory.github import Issue, IssueComment, SpecMarker, content_hash, render_marker
from factory.sandbox import Sandbox
from factory.schemas import SpecDraft, validate_spec
from factory.stages.thread import CurrentSpec, retry_message


class TriageHandoff(TypedDict):
    kind: Literal["spec"]
    comment_id: int
    hash: str
    version: int


class SpecSandbox(Sandbox, Protocol):
    pass


class SpecGitHub(Protocol):
    async def create_comment(self, number: int, body: str) -> IssueComment: ...
    async def update_comment(self, comment_id: int, body: str) -> IssueComment: ...
    async def remove_label(self, number: int, label: str) -> None: ...


SpecWriter = Callable[[str, SpecSandbox], Awaitable[SpecDraft]]


async def draft_spec(writer: SpecWriter, sandbox: SpecSandbox, message: str) -> tuple[SpecDraft, list[str]]:
    """Draft a spec and allow one validation-driven correction."""
    result = await sandbox.exec("git ls-files -z", timeout_seconds=60, as_runner=True)
    if result.exit_code != 0:
        raise RuntimeError(f"git ls-files exited {result.exit_code}: {result.stderr.strip()[-500:]}")
    existing = set(filter(None, result.stdout.split("\0")))
    draft = await writer(message, sandbox)
    validation = validate_spec(draft.spec, existing)
    if validation.errors:
        draft = await writer(retry_message(message, draft, validation.errors), sandbox)
        validation = validate_spec(draft.spec, existing)
    if validation.errors:
        detail = "; ".join(sanitize_inline(error) for error in validation.errors)
        raise ExpectedFailure(truncate(f"the spec failed validation twice: {detail}", 1500), "spec_invalid")
    return draft, validation.warnings


async def publish_spec(
    gh: SpecGitHub,
    issue: Issue,
    current: CurrentSpec | None,
    drafted: tuple[SpecDraft, list[str]],
) -> Outcome[TriageHandoff]:
    draft, warnings = drafted
    spec_data = draft.spec.model_dump(mode="json", by_alias=True)
    hash_value = content_hash(spec_data)
    if current and current.hash == hash_value:
        handoff: TriageHandoff = {
            "kind": "spec",
            "comment_id": current.comment.id,
            "hash": hash_value,
            "version": current.version,
        }
        return Outcome(
            "unchanged",
            f"the spec is unchanged (v{current.version}).",
            links=(StatusLink(f"Spec v{current.version}", current.comment.html_url),),
            data=handoff,
        )

    version = (current.version if current else 0) + 1
    labels = get_config().labels
    approval_removed = labels.spec_approved in issue.labels
    if approval_removed:
        await gh.remove_label(issue.number, labels.spec_approved)
    visible = render_spec(
        draft.spec,
        version=version,
        warnings=warnings,
        change_summary=draft.change_summary if current else None,
        approval_removed=approval_removed,
    )
    marker = render_marker(SpecMarker(spec=spec_data, hash=hash_value, version=version, notes=draft.notes))
    body = f"{visible}\n\n{marker}"
    comment = (
        await gh.update_comment(current.comment.id, body) if current else await gh.create_comment(issue.number, body)
    )
    note = f"; `{labels.spec_approved}` was removed, so reapply it to approve this version" if approval_removed else ""
    action = "revised the spec to" if current else "drafted spec"
    handoff = {"kind": "spec", "comment_id": comment.id, "hash": hash_value, "version": version}
    return Outcome(
        "revised" if current else "drafted",
        f"{action} v{version}{note}.",
        links=(StatusLink(f"Spec v{version}", comment.html_url),),
        data=handoff,
    )


def unique(values: Sequence[str], *, limit: int | None = None) -> list[str]:
    result = list(dict.fromkeys(values))
    return result if limit is None else result[:limit]
