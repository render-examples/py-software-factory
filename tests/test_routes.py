from __future__ import annotations

from typing import Any

import pytest

from factory.config import get_config
from factory.gateway import Ignore, Lookup, Start, ThreadState, WaitingRun, route
from factory.schemas import AutopilotInput, PrReviewInput, SpecToPrInput, TriageInput
from factory.threads import thread_key
from tests.fixtures import REPO, REPOSITORY_CASES, RepositoryCase

DELIVERY = "delivery-1"
ISSUE = thread_key(f"github:{REPO}#issue-12")
PR = thread_key(f"github:{REPO}#pr-34")
SHA = "a" * 40


def labels() -> Any:
    return get_config().labels


def envelope(**overrides: Any) -> dict[str, Any]:
    return {
        "repository": {"full_name": REPO},
        "installation": {"id": 7},
        "sender": {"login": "maintainer", "type": "User"},
        **overrides,
    }


def issue_labeled(label: str, **issue: Any) -> dict[str, Any]:
    return envelope(
        action="labeled",
        label={"name": label},
        issue={"number": 12, "updated_at": "2026-10-18T10:00:00Z", "labels": [{"name": label}], **issue},
    )


def pr_labeled(label: str, state: str = "open") -> dict[str, Any]:
    return envelope(
        action="labeled", label={"name": label}, pull_request={"number": 34, "state": state, "head": {"sha": SHA}}
    )


def comment(body: str, **issue: Any) -> dict[str, Any]:
    return envelope(
        action="created",
        issue={"number": 12, **issue},
        comment={"id": 99, "body": body},
    )


def push(**overrides: Any) -> dict[str, Any]:
    return envelope(**{"ref": "refs/heads/main", "after": SHA, "deleted": False, **overrides})


class TestStarts:
    def test_triage_label_on_an_issue_starts_triage(self) -> None:
        r = route("issues", issue_labeled(labels().triage), DELIVERY)
        assert r == Start(
            "triage", {"repo": REPO, "installation_id": 7, "issue": 12, "actor": "maintainer"}, DELIVERY, ISSUE
        )
        assert isinstance(r, Start)
        TriageInput.model_validate(r.input)

    def test_spec_approved_label_starts_spec_to_pr_with_a_label_approval(self) -> None:
        r = route("issues", issue_labeled(labels().spec_approved), DELIVERY)
        assert isinstance(r, Start)
        assert r.run == "spec-to-pr"
        assert r.idempotency_key == DELIVERY
        assert r.input == {
            "repo": REPO,
            "installation_id": 7,
            "issue": 12,
            "actor": "maintainer",
            "approval": {"kind": "label", "labeled_at": "2026-10-18T10:00:00Z"},
        }
        SpecToPrInput.model_validate(r.input)

    def test_autopilot_label_on_an_issue_starts_the_autopilot_task(self) -> None:
        r = route("issues", issue_labeled(labels().autopilot), DELIVERY)
        assert r == Start(
            "autopilot", {"repo": REPO, "installation_id": 7, "issue": 12, "actor": "maintainer"}, DELIVERY, ISSUE
        )
        assert isinstance(r, Start)
        AutopilotInput.model_validate(r.input)

    @pytest.mark.parametrize("task", ["triage", "autopilot"])
    def test_a_reply_resumes_the_waiting_root_task_in_reply_mode(self, task: str) -> None:
        r = route("issue_comment", comment("Here are the details."), DELIVERY)
        assert isinstance(r, Lookup)
        assert r.thread == ISSUE
        decided = r.decide(ThreadState(WaitingRun(task, "trn-root")))
        assert isinstance(decided, Start)
        assert decided.run == task
        assert decided.thread == ISSUE
        assert decided.input == {
            "repo": REPO,
            "installation_id": 7,
            "issue": 12,
            "actor": "maintainer",
            "comment_id": 99,
            "reply_to": "trn-root",
        }

    @pytest.mark.parametrize("waiting", [None, WaitingRun("spec-to-pr", "trn-root")])
    def test_a_comment_on_a_thread_that_is_not_waiting_starts_nothing(self, waiting: WaitingRun | None) -> None:
        r = route("issue_comment", comment("Unrelated note.", labels=[{"name": labels().triage}]), DELIVERY)
        assert isinstance(r, Lookup)
        assert r.decide(ThreadState(waiting)) == Ignore("thread is not waiting for a reply")

    def test_pr_review_label_on_an_open_pr_starts_pr_review(self) -> None:
        r = route("pull_request", pr_labeled(labels().pr_review), DELIVERY)
        expected = {"repo": REPO, "installation_id": 7, "pr": 34, "head_sha": SHA, "actor": "maintainer"}
        assert r == Start("pr-review", expected, DELIVERY, PR)
        assert isinstance(r, Start)
        PrReviewInput.model_validate(r.input)

    def test_cancel_command_on_an_issue_starts_cancel(self) -> None:
        r = route("issue_comment", comment(f"  {get_config().command_prefix} cancel  \nbecause"), DELIVERY)
        assert isinstance(r, Start)
        assert r.run == "cancel"
        assert r.input == {"repo": REPO, "installation_id": 7, "number": 12, "kind": "issue", "actor": "maintainer"}
        assert r.thread == ISSUE

    @pytest.mark.parametrize("case", REPOSITORY_CASES, ids=lambda c: c.repo)
    def test_routes_a_repository_only_from_its_own_base_branch(self, case: RepositoryCase) -> None:
        payload = push(repository={"full_name": case.repo}, ref=f"refs/heads/{case.base_branch}")
        assert route("push", payload, DELIVERY) == Start(
            "refresh-snapshot",
            {"repo": case.repo, "installation_id": 7, "sha": SHA, "actor": "maintainer"},
            f"{case.repo}@{SHA}",
        )
        assert isinstance(route("push", {**payload, "ref": "refs/heads/main"}, DELIVERY), Ignore)

    def test_push_to_the_base_branch_starts_refresh_snapshot_keyed_by_repo_at_sha(self) -> None:
        assert route("push", push(), DELIVERY) == Start(
            "refresh-snapshot",
            {"repo": REPO, "installation_id": 7, "sha": SHA, "actor": "maintainer"},
            f"{REPO}@{SHA}",
        )


class TestIgnores:
    @pytest.mark.parametrize(
        ("event", "payload"),
        [
            ("issues", {**issue_labeled("factory:triage"), "sender": {"login": "factory[bot]", "type": "Bot"}}),
            ("issues", {**issue_labeled("factory:triage"), "repository": {"full_name": "someone/else"}}),
            ("pull_request", pr_labeled("factory:pr-review", "closed")),
            ("push", push(ref="refs/heads/feature")),
            ("push", push(after="0" * 40)),
            ("push", push(deleted=True)),
            ("issues", {**issue_labeled("factory:triage"), "installation": {"id": "7"}}),
            ("ping", {}),
        ],
    )
    def test_does_not_start_work_for_untrusted_or_irrelevant_deliveries(self, event: str, payload: Any) -> None:
        assert isinstance(route(event, payload, DELIVERY), Ignore)

    def test_takes_the_target_repository_from_config_not_the_payload_alone(self) -> None:
        payload = issue_labeled(labels().triage)
        payload["repository"] = {"full_name": "acme/widget-evil"}
        assert route("issues", payload, DELIVERY) == Ignore("unconfigured repo")
