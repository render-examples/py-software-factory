from __future__ import annotations

import base64
import json
import re

from factory.github import (
    Actor,
    IssueComment,
    QuestionsMarker,
    ReviewMarker,
    SpecMarker,
    StatusMarker,
    content_hash,
    find_marked_comment,
    parse_marker,
    render_marker,
)

BOT = "software-factory[bot]"


def comment(id: int, login: str, body: str) -> IssueComment:
    kind = "Bot" if login.endswith("[bot]") else "User"
    return IssueComment(id, body, Actor(login, kind), "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "")


def encode(data: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


class TestMarkers:
    def test_round_trips_a_spec_with_its_content_hash_independent_of_key_order(self) -> None:
        spec = {"title": "Add dark mode", "criteria": ["toggle", "persists"], "meta": {"b": 1, "a": 2}}
        reordered = {"meta": {"a": 2, "b": 1}, "criteria": ["toggle", "persists"], "title": "Add dark mode"}
        assert content_hash(spec) == content_hash(reordered)
        marker = render_marker(SpecMarker(spec=spec, hash=content_hash(spec), version=2))
        assert parse_marker(marker, SpecMarker) == SpecMarker(spec=spec, hash=content_hash(spec), version=2)

    def test_hashes_as_the_typescript_factory_does(self) -> None:
        """Either implementation must accept the other's spec markers. Expected value from the TS contentHash."""
        value = {"b": [1, "é", None, True, 'line\nbreak \u2028 "q" \\ \u0001'], "a": {"y": 1.0, "x": "😀"}, "Z": 2.5}
        assert content_hash(value) == "50850f82a71d98ddaa6be8e2a1f599b49b1b17ceeebe8a1fdbfa3f38ad627206"

    def test_ignores_a_spec_whose_content_no_longer_matches_its_hash(self) -> None:
        spec = {"title": "Add dark mode"}
        tampered = {"spec": {"title": "Delete the database"}, "hash": content_hash(spec), "version": 1}
        assert parse_marker(f"<!-- software-factory:spec {encode(tampered)} -->", SpecMarker) is None

    def test_validates_question_and_review_marker_data(self) -> None:
        questions = render_marker(QuestionsMarker(round=2, questions=["Which API should this use?"]))
        review = render_marker(ReviewMarker(head_sha="a" * 40, run_id="trn-2"))
        assert parse_marker(questions, QuestionsMarker) == QuestionsMarker(
            round=2, questions=["Which API should this use?"]
        )
        assert parse_marker(review, ReviewMarker) == ReviewMarker(head_sha="a" * 40, run_id="trn-2")
        assert parse_marker(f"<!-- software-factory:questions {encode({'round': 2})} -->", QuestionsMarker) is None
        bad_review = encode({"headSha": "short", "runId": "trn-2"})
        assert parse_marker(f"<!-- software-factory:review {bad_review} -->", ReviewMarker) is None

    def test_writes_camel_case_json_as_the_typescript_factory_does(self) -> None:
        marker = render_marker(StatusMarker(run_id="trn-1", trace_id="trn-0", stage="triage", state="done"))
        encoded = re.search(r"status (\S+) -->", marker)
        assert encoded is not None
        data = json.loads(base64.urlsafe_b64decode(encoded.group(1) + "=="))
        assert data == {"runId": "trn-1", "traceId": "trn-0", "stage": "triage", "state": "done"}

    def test_returns_none_for_missing_other_kind_corrupted_or_invalid_markers(self) -> None:
        status = render_marker(StatusMarker(run_id="trn-1", stage="triage", state="done"))
        assert parse_marker("no marker here", StatusMarker) is None
        assert parse_marker(status, SpecMarker) is None
        assert parse_marker(re.sub(r" [A-Za-z0-9_-]+ -->", " AAAA -->", status), StatusMarker) is None
        wrong_shape = encode({"runId": "x", "stage": "triage", "state": "exploded"})
        assert parse_marker(f"<!-- software-factory:status {wrong_shape} -->", StatusMarker) is None

    def test_the_last_marker_of_a_kind_wins(self) -> None:
        first = render_marker(StatusMarker(run_id="trn-1", stage="triage", state="done"))
        second = render_marker(StatusMarker(run_id="trn-2", stage="triage", state="done"))
        parsed = parse_marker(f"{first}\r\nedited\n{second}", StatusMarker)
        assert parsed is not None
        assert parsed.run_id == "trn-2"


class TestFindMarkedComment:
    @staticmethod
    def marker(run_id: str) -> str:
        return render_marker(StatusMarker(run_id=run_id, stage="triage", state="running"))

    def test_ignores_markers_pasted_by_anyone_else(self) -> None:
        comments = [
            comment(1, BOT, self.marker("trn-real")),
            comment(2, "mallory", f"copied: {self.marker('trn-forged')}"),
        ]
        found = find_marked_comment(comments, StatusMarker, BOT)
        assert found is not None
        assert found[1].run_id == "trn-real"
        assert find_marked_comment([comments[1]], StatusMarker, BOT) is None
        assert find_marked_comment([comment(4, "software-factory", self.marker("trn-x"))], StatusMarker, BOT) is None
