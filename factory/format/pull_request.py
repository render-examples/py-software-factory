from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from factory.format.spec import acceptance_checklist
from factory.format.text import BODY_MAX_CHARS, bullets, code_span, fit_around, sanitize, sanitize_inline
from factory.schemas import Implementation, Spec

_TITLE_MAX_CHARS = 256
"""GitHub's limit on a pull request title."""


@dataclass(frozen=True)
class VerifyResult:
    """A verify command as the workflow ran it."""

    command: str
    passed: bool
    duration_ms: int | None = None


def pull_request_title(spec: Spec) -> str:
    title = sanitize_inline(spec.title)
    return title if len(title) <= _TITLE_MAX_CHARS else f"{title[: _TITLE_MAX_CHARS - 1].rstrip()}…"


def pull_request_body(
    *,
    issue: int,
    spec: Spec,
    implementation: Implementation,
    verification: Sequence[VerifyResult] | None = None,
    spec_url: str | None = None,
) -> str:
    """The draft PR's body. The caller appends its hidden marker.

    `verification` is the workflow's own verify run. Without it, the implementer's self-reported results are shown
    as such.
    """
    sections = [
        f"## Summary\n\n{sanitize(implementation.summary)}",
        f"## Acceptance criteria\n\n{acceptance_checklist(spec)}",
    ]
    if verification is not None:
        sections.append(f"## Verification\n\n{verification_table(verification)}")
    elif implementation.verification:
        rows = [
            f"| {_result(v.passed)} | {_escape_cell(code_span(v.command))} | {_escape_cell(sanitize_inline(v.notes))} |"
            for v in implementation.verification
        ]
        table = "| Result | Command | Notes |\n| --- | --- | --- |\n" + "\n".join(rows)
        sections.append(f"## Verification (reported by the implementer)\n\n{table}")
    if implementation.follow_ups:
        sections.append(f"## Follow-ups\n\n{bullets(implementation.follow_ups)}")

    links = f"\n\n[Specification]({spec_url})" if spec_url else ""
    return fit_around(
        f"Closes #{issue}.\n\n",
        "\n\n".join(sections),
        f"{links}\n\n---\n\n_Opened as a draft by the factory. Review it before marking it ready._",
        BODY_MAX_CHARS,
    )


def verification_table(results: Sequence[VerifyResult]) -> str:
    """A Markdown table of verify results."""
    if not results:
        return "No verify commands are configured."
    rows = [
        f"| {_result(r.passed)} | {_escape_cell(code_span(r.command))} | "
        f"{'' if r.duration_ms is None else f'{r.duration_ms / 1000:.1f}s'} |"
        for r in results
    ]
    return "\n".join(["| Result | Command | Duration |", "| --- | --- | --- |", *rows])


def _result(passed: bool) -> str:
    return "✅ passed" if passed else "❌ failed"


def _escape_cell(text: str) -> str:
    """Escapes characters that end a table cell."""
    return text.replace("|", "\\|")
