from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from factory.schemas import (
    AutopilotInput,
    Implementation,
    Review,
    Spec,
    SpecDraft,
    SpecToPrInput,
    TriageDecision,
    issues,
    validate_spec,
)


def spec_json(**overrides: Any) -> dict[str, Any]:
    return {
        "title": "Add a dark mode toggle",
        "summary": "Let users switch the site to a dark theme.",
        "problem": "The site is bright at night.",
        "goals": ["A toggle in the header"],
        "nonGoals": [],
        "acceptanceCriteria": [
            {"id": "AC-1", "text": "Clicking the toggle switches the theme", "verification": "automated"},
            {"id": "AC-2", "text": "The choice survives a reload", "verification": "manual"},
        ],
        "approach": "Add a ThemeToggle component.",
        "affectedAreas": [
            {"path": "src/App.tsx", "change": "render the toggle"},
            {"path": "src/components/ThemeToggle.tsx", "change": "new file"},
        ],
        "testPlan": "Unit test the toggle.",
        "risks": [],
        "assumptions": [],
        "openQuestions": [],
        **overrides,
    }


def errors_of(model: type[BaseModel], data: Any) -> list[str]:
    try:
        model.model_validate(data, by_alias=True, by_name=False)
    except ValidationError as error:
        return issues(error)
    return []


class TestTriageDecision:
    base: dict[str, Any] = {"rationale": "r", "findings": "f", "relevantPaths": []}  # noqa: RUF012

    def test_requires_questions_exactly_when_asking(self) -> None:
        assert errors_of(TriageDecision, {**self.base, "decision": "ask", "questions": ["Which page?"]}) == []
        assert errors_of(TriageDecision, {**self.base, "decision": "ask", "questions": []}) == [
            'questions: decision "ask" needs at least one question'
        ]
        assert errors_of(TriageDecision, {**self.base, "decision": "draft", "questions": ["Why?"]}) == [
            'questions: decision "draft" takes no questions'
        ]
        assert errors_of(TriageDecision, {**self.base, "decision": "decline", "questions": []}) == []

    def test_rejects_a_misspelled_field_by_name_so_the_agent_can_fix_it(self) -> None:
        misspelled: dict[str, Any] = {
            "rationale": "r",
            "findings": "f",
            "decision": "draft",
            "questions": [],
            "relevant_paths": [],
        }
        assert errors_of(TriageDecision, misspelled) == ["relevant_paths: Extra inputs are not permitted"]

    def test_defaults_advisory_relevant_paths_when_the_model_omits_them(self) -> None:
        decision = TriageDecision.parse_untrusted(
            {"rationale": "r", "findings": "f", "decision": "draft", "questions": []}
        )
        assert decision.relevant_paths == []

    def test_does_not_coerce_types(self) -> None:
        assert errors_of(TriageDecision, {**self.base, "decision": "draft", "questions": [], "rationale": 5}) != []


class TestTaskInputs:
    base: dict[str, Any] = {"repo": "acme/web", "installation_id": 7, "issue": 12, "actor": "maintainer"}  # noqa: RUF012

    def test_accepts_an_autopilot_start_and_an_optional_human_reply(self) -> None:
        AutopilotInput.model_validate(self.base)
        AutopilotInput.model_validate({**self.base, "comment_id": 99})

    def test_distinguishes_manual_and_autopilot_spec_approval(self) -> None:
        label = SpecToPrInput.model_validate(
            {**self.base, "approval": {"kind": "label", "labeled_at": "2026-10-25T12:00:00Z"}}
        )
        assert label.approval.kind == "label"
        auto = SpecToPrInput.model_validate(
            {**self.base, "approval": {"kind": "autopilot", "comment_id": 100, "hash": "a" * 64, "version": 1}}
        )
        assert auto.approval.kind == "autopilot"

    def test_rejects_a_label_approval_without_a_timezone(self) -> None:
        with pytest.raises(ValidationError):
            SpecToPrInput.model_validate(
                {**self.base, "approval": {"kind": "label", "labeled_at": "2026-10-25T12:00:00"}}
            )

    @pytest.mark.parametrize("repository", ["../web", "acme/..", "acmé/web", "acme/web/extra"])
    def test_rejects_unsafe_repository_names(self, repository: str) -> None:
        with pytest.raises(ValidationError, match="repository must be ASCII"):
            AutopilotInput.model_validate({**self.base, "repo": repository})

    def test_round_trips_through_json(self) -> None:
        start = SpecToPrInput.model_validate(
            {**self.base, "approval": {"kind": "label", "labeled_at": "2026-10-25T12:00:00Z"}}
        )
        assert SpecToPrInput.model_validate(start.model_dump(mode="json")) == start


class TestSpec:
    def test_accepts_a_full_spec_and_a_draft_with_or_without_a_change_summary(self) -> None:
        assert Spec.parse_untrusted(spec_json()).model_dump(mode="json") == spec_json()
        notes: dict[str, Any] = {"findings": "", "relevantPaths": []}
        SpecDraft.parse_untrusted({"spec": spec_json(), "notes": notes})
        SpecDraft.parse_untrusted({"spec": spec_json(), "notes": notes, "changeSummary": "Narrowed AC-2."})

    def test_rejects_duplicate_acceptance_criteria(self) -> None:
        duplicate = Spec.parse_untrusted(
            spec_json(
                acceptanceCriteria=[
                    {"id": "AC-1", "text": "One", "verification": "automated"},
                    {"id": "AC-1", "text": "Two", "verification": "manual"},
                ]
            )
        )
        assert validate_spec(duplicate).errors == ["Duplicate acceptance criterion IDs: AC-1"]

    def test_rejects_paths_outside_the_repository_and_warns_about_proposed_files(self) -> None:
        spec = Spec.parse_untrusted(
            spec_json(
                affectedAreas=[{"path": "../secret", "change": "read it"}, {"path": "src/new.ts", "change": "new file"}]
            )
        )
        result = validate_spec(spec, {"src/App.tsx"})
        assert "../secret is not a repository-relative path" in result.errors
        assert "src/new.ts does not exist yet" in result.warnings

    def test_rejects_a_spec_too_large_for_its_github_marker(self) -> None:
        spec = Spec.parse_untrusted(spec_json()).model_copy(update={"approach": "x" * 20_000, "test_plan": "x" * 4000})
        result = validate_spec(spec)
        assert len(result.errors) == 1
        assert "characters as JSON" in result.errors[0]


class TestReviewContracts:
    def test_has_no_approval_field_and_caps_inline_comments_at_50(self) -> None:
        comment = {"file": "a.ts", "line": 3, "severity": "low", "title": "t", "body": "b"}
        Review.parse_untrusted({"summary": "s", "riskTier": "low", "comments": [comment]})
        with pytest.raises(ValidationError):
            Review.parse_untrusted({"summary": "s", "riskTier": "low", "comments": [comment] * 51})
        assert list(Review.model_json_schema(by_alias=True)["properties"]) == ["summary", "riskTier", "comments"]


@pytest.mark.parametrize("model", [TriageDecision, Spec, SpecDraft, Implementation, Review])
def test_every_agent_output_is_strict_and_bounded(model: type[BaseModel]) -> None:
    """Unknown keys are rejected, and every string and array in the output schema has an upper bound."""
    schema = model.model_json_schema(by_alias=True)
    unbounded: list[str] = []

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        node_dict: dict[str, Any] = node  # pyright: ignore[reportUnknownVariableType]
        if "$ref" in node_dict:
            walk(schema["$defs"][node_dict["$ref"].split("/")[-1]], path)
        if node_dict.get("type") == "object" and "properties" in node_dict:
            assert node_dict.get("additionalProperties") is False, f"{model.__name__}{path} allows unknown keys"
            for key, child in node_dict["properties"].items():
                walk(child, f"{path}.{key}")
        if node_dict.get("type") == "string" and "maxLength" not in node_dict and "enum" not in node_dict:
            unbounded.append(path)
        if node_dict.get("type") == "array":
            if "maxItems" not in node_dict:
                unbounded.append(path)
            walk(node_dict.get("items"), f"{path}[]")
        for option in node_dict.get("anyOf", []):
            walk(option, path)

    walk(schema, "")
    assert unbounded == []
