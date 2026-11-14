"""Loading the exact spec a maintainer approved (invariant 9)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pydantic import ValidationError

from factory.claims import ExpectedFailure
from factory.config import get_config
from factory.github import IssueComment, LabelEvent, SpecMarker, find_marked_comment
from factory.schemas import AutopilotApproval, LabelApproval, Spec


class ApprovalGitHub(Protocol):
    async def bot_login(self) -> str: ...
    async def list_issue_comments(self, number: int) -> list[IssueComment]: ...
    async def get_label_events(self, number: int, label: str) -> list[LabelEvent]: ...


@dataclass(frozen=True)
class ApprovedSpec:
    spec: Spec
    comment: IssueComment


async def load_approved_spec(
    gh: ApprovalGitHub, issue: int, approval: LabelApproval | AutopilotApproval
) -> ApprovedSpec:
    """Loads the exact approved spec, or raises an ExpectedFailure.

    Manual runs compare the spec comment's last edit with the newest approval label event. Autopilot names the
    App-authored spec comment, hash, and version the Triage stage produced, and all three must still match.
    """
    labels = get_config().labels
    found = find_marked_comment(await gh.list_issue_comments(issue), SpecMarker, await gh.bot_login())
    if found is None:
        raise ExpectedFailure(
            f"there is no spec on this issue. Add the `{labels.triage}` label to draft one, then approve it with "
            f"`{labels.spec_approved}`.",
            "no_spec",
        )
    comment, marker = found

    if isinstance(approval, LabelApproval):
        events = await gh.get_label_events(issue, labels.spec_approved)
        approved_at = datetime.fromisoformat(events[-1].created_at) if events else approval.labeled_at
        if datetime.fromisoformat(comment.updated_at) > approved_at:
            raise ExpectedFailure(
                f"the spec changed after it was approved. Review it and reapply the `{labels.spec_approved}` label.",
                "spec_changed",
            )
    elif (comment.id, marker.hash, marker.version) != (approval.comment_id, approval.hash, approval.version):
        raise ExpectedFailure("the spec changed after Triage returned it to autopilot.", "spec_changed")

    try:
        spec = Spec.parse_untrusted(marker.spec)
    except ValidationError as error:
        raise ExpectedFailure(
            f"the spec comment doesn't hold a valid spec. Add the `{labels.triage}` label to redraft it.",
            "spec_unreadable",
        ) from error
    return ApprovedSpec(spec, comment)
