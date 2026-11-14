"""Pass model text through here before writing it to GitHub."""

from factory.format.pull_request import VerifyResult, pull_request_body, pull_request_title, verification_table
from factory.format.review import ReviewPayload, render_review
from factory.format.spec import acceptance_checklist, render_spec, render_spec_sections
from factory.format.text import (
    BODY_MAX_CHARS,
    COMMENT_MAX_CHARS,
    code_span,
    fenced,
    sanitize,
    sanitize_inline,
    truncate,
    verbatim_code_span,
)
from factory.format.triage import triage_decline_body, triage_questions_body

__all__ = [
    "BODY_MAX_CHARS",
    "COMMENT_MAX_CHARS",
    "ReviewPayload",
    "VerifyResult",
    "acceptance_checklist",
    "code_span",
    "fenced",
    "pull_request_body",
    "pull_request_title",
    "render_review",
    "render_spec",
    "render_spec_sections",
    "sanitize",
    "sanitize_inline",
    "triage_decline_body",
    "triage_questions_body",
    "truncate",
    "verbatim_code_span",
    "verification_table",
]
