"""Task inputs. Every one names its repository and the App installation to act through."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator

from factory.identifiers import is_valid_repo_name
from factory.schemas.base import TaskInput

PositiveInt = Annotated[int, Field(gt=0)]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
ActorLogin = Annotated[str, Field(min_length=1)]


class RepoInput(TaskInput):
    repo: str = Field(min_length=3, max_length=200)
    installation_id: PositiveInt
    actor: ActorLogin

    @field_validator("repo")
    @classmethod
    def _repository_name(cls, value: str) -> str:
        if not is_valid_repo_name(value):
            raise ValueError('repository must be ASCII "owner/name" with no dot segments')
        return value


class TriageInput(RepoInput):
    issue: PositiveInt
    comment_id: PositiveInt | None = None
    """Set when a human reply started this round."""


class AutopilotInput(RepoInput):
    issue: PositiveInt
    comment_id: PositiveInt | None = None
    """Set when a human reply restarted an autopilot run that had asked questions."""


class LabelApproval(TaskInput):
    kind: Literal["label"] = "label"
    labeled_at: AwareDatetime = Field(strict=False)
    """When the approval label was applied; the spec must not have changed after it."""


class AutopilotApproval(TaskInput):
    kind: Literal["autopilot"] = "autopilot"
    comment_id: PositiveInt
    """The exact App-authored spec the Triage stage posted."""
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: PositiveInt


SpecApproval = Annotated[LabelApproval | AutopilotApproval, Field(discriminator="kind")]


class SpecToPrInput(RepoInput):
    issue: PositiveInt
    approval: SpecApproval


class PrReviewInput(RepoInput):
    pr: PositiveInt
    head_sha: Sha
    autopilot_issue: PositiveInt | None = None
    """Keeps an autopilot review under the issue-level root claim."""


class CancelInput(RepoInput):
    number: PositiveInt
    """Issue or PR number."""
    kind: Literal["issue", "pr"]


class RefreshSnapshotInput(RepoInput):
    sha: Sha
