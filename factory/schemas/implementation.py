from __future__ import annotations

from typing import Annotated

from pydantic import Field

from factory.schemas.base import StrictModel


class VerificationRun(StrictModel):
    """One verify command as the implementer ran it. Self-reported: the workflow reruns verify and trusts only its
    own run."""

    command: str = Field(min_length=1, max_length=2000)
    passed: bool
    notes: str = Field(
        max_length=2000,
        description="What failed and what you did about it, or why you could not run it. Empty when it passed cleanly.",
    )


class Implementation(StrictModel):
    """The implementer's submission, for a first run and for a repair run alike."""

    summary: str = Field(
        min_length=1, max_length=4000, description="What you changed and why, for the pull request description."
    )
    files_changed: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        max_length=500, description="Repository-relative paths you created, modified, or deleted."
    )
    verification: list[VerificationRun] = Field(
        max_length=50, description="Every verify command, in order, with its last result."
    )
    follow_ups: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        max_length=20, description="Work the spec implies but you left out, for the maintainer."
    )
