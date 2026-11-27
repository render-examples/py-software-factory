"""Task inputs. Every one names its repository and the App installation to act through."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator

from factory.identifiers import is_valid_repo_name
from factory.schemas.base import TaskInput

PositiveInt = Annotated[int, Field(gt=0)]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
ActorLogin = Annotated[str, Field(min_length=1)]


class TraceContext(TaskInput):
    """The W3C trace context of whatever started the task, so its run continues that trace. Telemetry only."""

    traceparent: str = Field(pattern=r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


class RepoInput(TaskInput):
    repo: str = Field(min_length=3, max_length=200)
    installation_id: PositiveInt
    actor: ActorLogin
    trace_context: TraceContext | None = None

    @field_validator("repo")
    @classmethod
    def _repository_name(cls, value: str) -> str:
        if not is_valid_repo_name(value):
            raise ValueError('repository must be ASCII "owner/name" with no dot segments')
        return value


ReplyTo = Annotated[str, Field(min_length=1, max_length=200)]


class TriageInput(RepoInput):
    issue: PositiveInt
    comment_id: PositiveInt | None = None
    """Set when a human reply started this round."""
    reply_to: ReplyTo | None = None
    """The waiting run the reply answers. The run resumes only while that run is still the issue's latest."""


class AutopilotInput(RepoInput):
    issue: PositiveInt
    comment_id: PositiveInt | None = None
    """Set when a human reply restarted an autopilot run that had asked questions."""
    reply_to: ReplyTo | None = None
    """The waiting run the reply answers. The run resumes only while that run is still the issue's latest."""


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


class CancelInput(RepoInput):
    number: PositiveInt
    """Issue or PR number."""
    kind: Literal["issue", "pr"]


class RefreshSnapshotInput(RepoInput):
    sha: Sha
