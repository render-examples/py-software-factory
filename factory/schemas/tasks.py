"""Task inputs. Every one names its repository and the App installation to act through."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from factory.schemas.base import TaskInput

PositiveInt = Annotated[int, Field(gt=0)]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
ActorLogin = Annotated[str, Field(min_length=1)]


class RepoInput(TaskInput):
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    installation_id: PositiveInt


class TriageInput(RepoInput):
    issue: PositiveInt
    actor: ActorLogin
    comment_id: PositiveInt | None = None
    """Set when a human reply started this round."""


class AutopilotInput(RepoInput):
    issue: PositiveInt
    actor: ActorLogin
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
    actor: ActorLogin
    approval: SpecApproval


class PrReviewInput(RepoInput):
    pr: PositiveInt
    head_sha: Sha
    actor: ActorLogin
    autopilot_issue: PositiveInt | None = None
    """Keeps an autopilot review under the issue-level root claim."""


class CancelInput(RepoInput):
    number: PositiveInt
    """Issue or PR number."""
    kind: Literal["issue", "pr"]
    actor: ActorLogin


class RefreshSnapshotInput(RepoInput):
    sha: Sha
    actor: ActorLogin
