from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, ValidationInfo, field_validator

from factory.schemas.base import StrictModel


class TriageDecision(StrictModel):
    """The triage agent's submission: whether a competent engineer would know what to build."""

    decision: Literal["ask", "draft", "decline"] = Field(
        description='"draft" by default; "ask" only when the intent is unclear; '
        '"decline" for spam, duplicates, or out of scope.'
    )
    questions: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        max_length=5, description='Questions for the maintainer. Non-empty exactly when decision is "ask".'
    )
    rationale: str = Field(
        min_length=1, max_length=4000, description="Why you decided this. Shown to the maintainer when you decline."
    )
    findings: str = Field(
        max_length=8000, description="What you learned from the repository, kept for the spec writer and later rounds."
    )
    relevant_paths: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        default_factory=list[str],
        max_length=50,
        description="Repository-relative paths that matter for the change, when any.",
    )

    @field_validator("questions")
    @classmethod
    def _questions_match_decision(cls, questions: list[str], info: ValidationInfo) -> list[str]:
        decision = info.data.get("decision")
        if decision == "ask" and not questions:
            raise ValueError('decision "ask" needs at least one question')
        if decision is not None and decision != "ask" and questions:
            raise ValueError(f'decision "{decision}" takes no questions')
        return questions
