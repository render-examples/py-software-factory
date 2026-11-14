from __future__ import annotations

from typing import Final, Literal

from pydantic import Field

from factory.schemas.base import StrictModel

Severity = Literal["critical", "high", "medium", "low"]
SEVERITIES: Final[tuple[Severity, ...]] = ("critical", "high", "medium", "low")


class ReviewComment(StrictModel):
    file: str = Field(
        min_length=1, max_length=1000, description="Repository-relative path of a file the pull request changes."
    )
    line: int = Field(ge=1, description="Line number in the new version of the file.")
    severity: Severity
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)


class Review(StrictModel):
    """The PR reviewer's submission. It summarizes and assesses risk; it never approves or requests changes."""

    summary: str = Field(
        min_length=1,
        max_length=8000,
        description="Markdown overview of the review, including findings that don't fit an inline comment.",
    )
    risk_tier: Literal["trivial", "low", "medium", "high"] = Field(description="The overall assessment.")
    comments: list[ReviewComment] = Field(
        max_length=50, description="Actionable inline comments, most important first."
    )
