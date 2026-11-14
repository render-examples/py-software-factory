from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from factory.claims import ExpectedFailure
from factory.github import Actor, IssueComment, LabelEvent, SpecMarker, content_hash, render_marker
from factory.schemas import AutopilotApproval, LabelApproval
from factory.stages import load_approved_spec
from tests.test_schemas import spec_json

BOT = "software-factory[bot]"
SPEC = spec_json()
LABELED_AT = "2026-10-18T10:00:00Z"


class FakeGitHub:
    def __init__(
        self,
        *,
        spec: Any = SPEC,
        spec_updated_at: str = "2026-10-18T09:00:00Z",
        label_events: tuple[str, ...] = (LABELED_AT,),
        author: str = BOT,
    ) -> None:
        self.label_event_reads = 0
        self.comments: list[IssueComment] = []
        if spec is not None:
            marker = render_marker(SpecMarker(spec=spec, hash=content_hash(spec), version=1))
            self.comments.append(IssueComment(1, f"## Spec\n\n{marker}", Actor(author, "Bot"), "", spec_updated_at, ""))
        self.label_events = [LabelEvent("factory:spec-approved", at, None) for at in label_events]

    async def bot_login(self) -> str:
        return BOT

    async def list_issue_comments(self, number: int) -> list[IssueComment]:
        return self.comments

    async def get_label_events(self, number: int, label: str) -> list[LabelEvent]:
        self.label_event_reads += 1
        return self.label_events


LABEL = LabelApproval(labeled_at=datetime.fromisoformat(LABELED_AT))


async def outcome_of(gh: FakeGitHub, approval: LabelApproval | AutopilotApproval = LABEL) -> str:
    with pytest.raises(ExpectedFailure) as caught:
        await load_approved_spec(gh, 12, approval)
    return caught.value.outcome


async def test_loads_a_spec_approved_after_its_last_edit() -> None:
    approved = await load_approved_spec(FakeGitHub(), 12, LABEL)
    assert approved.spec.title == SPEC["title"]
    assert approved.comment.id == 1


async def test_refuses_when_there_is_no_spec_comment() -> None:
    assert await outcome_of(FakeGitHub(spec=None)) == "no_spec"


async def test_ignores_a_spec_marker_someone_else_pasted() -> None:
    assert await outcome_of(FakeGitHub(author="mallory")) == "no_spec"


async def test_refuses_a_spec_edited_after_the_newest_approval_label_event() -> None:
    gh = FakeGitHub(
        label_events=("2026-10-18T10:00:00Z", "2026-10-18T19:30:00Z"), spec_updated_at="2026-10-18T19:45:00Z"
    )
    assert await outcome_of(gh) == "spec_changed"


async def test_uses_the_newest_label_event_not_the_webhooks_timestamp() -> None:
    gh = FakeGitHub(label_events=("2026-10-18T20:00:00Z",), spec_updated_at="2026-10-18T19:45:00Z")
    assert (await load_approved_spec(gh, 12, LABEL)).spec.title == SPEC["title"]


async def test_accepts_the_exact_spec_handed_off_by_an_autopilot_triage_stage() -> None:
    gh = FakeGitHub(spec_updated_at="2026-10-18T23:00:00Z")
    approval = AutopilotApproval(comment_id=1, hash=content_hash(SPEC), version=1)
    assert (await load_approved_spec(gh, 12, approval)).spec.title == SPEC["title"]
    assert gh.label_event_reads == 0


@pytest.mark.parametrize(
    "approval",
    [
        AutopilotApproval(comment_id=1, hash="f" * 64, version=1),
        AutopilotApproval(comment_id=2, hash=content_hash(SPEC), version=1),
        AutopilotApproval(comment_id=1, hash=content_hash(SPEC), version=2),
    ],
)
async def test_refuses_an_autopilot_handoff_when_the_current_spec_does_not_match(approval: AutopilotApproval) -> None:
    assert await outcome_of(FakeGitHub(), approval) == "spec_changed"


async def test_refuses_a_marker_whose_spec_is_not_a_valid_spec() -> None:
    assert await outcome_of(FakeGitHub(spec={"title": "not a spec"})) == "spec_unreadable"
