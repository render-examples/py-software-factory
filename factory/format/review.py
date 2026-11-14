from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from factory.format.pull_request import VerifyResult, verification_table
from factory.format.text import BODY_MAX_CHARS, code_span, fit_around, sanitize, sanitize_inline
from factory.github.types import InlineComment
from factory.schemas import SEVERITIES, Review, ReviewComment, Severity

_MAX_INLINE_COMMENTS = 50
"""Inline comments posted with one review; the rest go in the body."""
_INLINE_BODY_MAX_CHARS = 8_000


@dataclass(frozen=True)
class ReviewPayload:
    body: str
    comments: list[InlineComment]
    event: Literal["COMMENT"] = "COMMENT"
    """The only review event the factory posts: it never approves or requests changes."""


def render_review(
    review: Review,
    *,
    verification: Sequence[VerifyResult] | None = None,
    is_commentable: Callable[[str, int], bool] | None = None,
) -> ReviewPayload:
    """The review as a COMMENT review: a body and at most 50 inline comments, most severe first.

    `verification` is the workflow's verify run at the PR head. `is_commentable` says whether GitHub accepts an
    inline comment at a line of the PR's diff (it rejects the whole review otherwise); comments it refuses move to
    the body. Without it, every comment is posted inline.
    """
    ordered = sorted(review.comments, key=lambda c: SEVERITIES.index(c.severity))

    inline: list[ReviewComment] = []
    overflow: list[ReviewComment] = []
    for comment in ordered:
        placeable = is_commentable(comment.file, comment.line) if is_commentable else True
        if placeable and len(inline) < _MAX_INLINE_COMMENTS:
            inline.append(comment)
        else:
            overflow.append(comment)

    counts = [f"{n} {s}" for s in SEVERITIES if (n := sum(1 for c in review.comments if c.severity == s))]
    findings = ", ".join(counts) if counts else "none"
    head = (
        f"## Factory review\n\n**Risk:** {review.risk_tier} · **Findings:** {findings} · "
        f"**Inline comments:** {len(inline)}\n\n"
    )

    sections = [sanitize(review.summary)]
    if verification is not None:
        sections.append(f"### Verification\n\n{verification_table(verification)}")
    if overflow:
        items = "\n".join(
            f"- **{_label(c.severity)}** {code_span(f'{c.file}:{c.line}')}: {sanitize_inline(c.title)}"
            for c in overflow
        )
        sections.append(f"### More findings\n\nThese couldn't be placed as inline comments.\n\n{items}")

    tail = "\n\n---\n\n_The factory reviews by comment only. It never approves or requests changes; a person decides._"
    return ReviewPayload(
        body=fit_around(head, "\n\n".join(sections), tail, BODY_MAX_CHARS),
        comments=[InlineComment(path=c.file, line=c.line, body=_inline_body(c)) for c in inline],
    )


def _label(severity: Severity) -> str:
    return severity.capitalize()


def _inline_body(comment: ReviewComment) -> str:
    head = f"**{_label(comment.severity)}**: {sanitize_inline(comment.title)}\n\n"
    return fit_around(head, sanitize(comment.body), "", _INLINE_BODY_MAX_CHARS)
