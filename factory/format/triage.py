from __future__ import annotations

from collections.abc import Sequence

from factory.config import get_config
from factory.format.text import BODY_MAX_CHARS, code_span, fit_around, sanitize, sanitize_inline


def triage_questions_body(questions: Sequence[str]) -> str:
    """The comment body when triage asks the maintainer questions."""
    numbered = "\n".join(f"{i}. {sanitize_inline(q)}" for i, q in enumerate(questions, start=1))
    return fit_around(
        "The factory needs more detail before it can write a specification:\n\n",
        numbered,
        "\n\n**Next step:** reply to this issue with your answers, and the factory will continue.",
        BODY_MAX_CHARS,
    )


def triage_decline_body(rationale: str) -> str:
    """The comment body when triage declines the issue."""
    return fit_around(
        "The factory declined this issue:\n\n",
        sanitize(rationale),
        f"\n\nTo try again, apply the {code_span(get_config().labels.triage)} label again.",
        BODY_MAX_CHARS,
    )
