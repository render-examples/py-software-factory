from __future__ import annotations

from collections.abc import Sequence

from factory.config import get_config
from factory.format.text import bullets, code_span, fit_around, sanitize, sanitize_inline, truncate
from factory.schemas import Spec

_SPEC_BODY_MAX_CHARS = 32_000
"""The rendered spec comment's budget: the caller's marker carries the spec again as JSON, up to
SPEC_MAX_JSON_CHARS."""


def render_spec_sections(spec: Spec) -> str:
    """The spec's sections as Markdown, with no heading or instructions: for the spec comment and the implementer's
    message."""
    sections = [f"**{sanitize_inline(spec.title)}**", sanitize(spec.summary)]

    def section(heading: str, body: str) -> None:
        if body.strip():
            sections.append(f"### {heading}\n\n{body}")

    section("Problem", sanitize(spec.problem))
    section("Goals", bullets(spec.goals))
    section("Non-goals", bullets(spec.non_goals))
    section("Acceptance criteria", acceptance_checklist(spec))
    section("Approach", sanitize(spec.approach))
    section(
        "Affected areas", "\n".join(f"- {code_span(a.path)}: {sanitize_inline(a.change)}" for a in spec.affected_areas)
    )
    section("Test plan", sanitize(spec.test_plan))
    section("Risks", bullets(spec.risks))
    section("Assumptions", bullets(spec.assumptions))
    section("Open questions", bullets(spec.open_questions))
    return "\n\n".join(sections)


def acceptance_checklist(spec: Spec) -> str:
    """The acceptance criteria as a Markdown task list."""
    return "\n".join(
        f"- [ ] **{sanitize_inline(c.id)}**: {sanitize_inline(c.text)} _({c.verification})_"
        for c in spec.acceptance_criteria
    )


def render_spec(
    spec: Spec,
    *,
    version: int | None = None,
    warnings: Sequence[str] = (),
    change_summary: str | None = None,
) -> str:
    """The spec comment's visible body. The caller appends the hidden marker that carries the spec as JSON."""
    approve = code_span(get_config().labels.spec_approved)
    suffix = f" v{version}" if version else ""
    head = f"## Specification{suffix}\n\n"

    extras: list[str] = []
    if warnings:
        extras.append(f"### Warnings\n\n{truncate(bullets(warnings), 4000)}")
    if change_summary and change_summary.strip():
        extras.append(f"### Changes{' in' + suffix if version else ''}\n\n{truncate(sanitize(change_summary), 4000)}")
    tail = "\n\n".join(
        [
            "",
            *extras,
            "---",
            f"**Next step:** reply to this issue with feedback, or apply the {approve} label to approve this version.",
        ]
    )
    return fit_around(head, render_spec_sections(spec), tail, _SPEC_BODY_MAX_CHARS)
