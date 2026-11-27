from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import Field

from factory.schemas.base import StrictModel

Item = Annotated[str, Field(min_length=1, max_length=1000)]


class AcceptanceCriterion(StrictModel):
    id: str = Field(pattern=r"^AC-[1-9]\d*$", max_length=20, description='Criterion IDs look like "AC-1".')
    text: str = Field(min_length=1, max_length=1000)
    verification: Literal["automated", "manual"]


class AffectedArea(StrictModel):
    path: str = Field(
        min_length=1, max_length=300, description="Repository-relative path. New files are fine; say so in change."
    )
    change: str = Field(min_length=1, max_length=1000)


class Spec(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2000)
    problem: str = Field(max_length=4000)
    goals: list[Item] = Field(max_length=20)
    non_goals: list[Item] = Field(max_length=20)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1, max_length=30)
    approach: str = Field(max_length=8000)
    affected_areas: list[AffectedArea] = Field(max_length=50)
    test_plan: str = Field(max_length=4000)
    risks: list[Item] = Field(max_length=20)
    assumptions: list[Item] = Field(max_length=20)
    open_questions: list[Item] = Field(max_length=20)


class SpecNotes(StrictModel):
    """What the spec writer learned, kept with the spec so later rounds skip the research."""

    findings: str = Field(max_length=8000)
    relevant_paths: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(max_length=50)


class SpecDraft(StrictModel):
    """The spec writer's submission."""

    spec: Spec
    notes: SpecNotes
    change_summary: str | None = Field(
        default=None,
        max_length=2000,
        description="Only when revising a previous version: what changed and why.",
    )


SPEC_MAX_JSON_CHARS = 20_000


@dataclass
class SpecValidation:
    errors: list[str] = field(default_factory=list[str])
    warnings: list[str] = field(default_factory=list[str])


def validate_spec(spec: Spec, existing_paths: set[str] | frozenset[str] | None = None) -> SpecValidation:
    result = SpecValidation()

    ids = [c.id for c in spec.acceptance_criteria]
    duplicate_ids = _duplicates(ids)
    if duplicate_ids:
        result.errors.append(f"Duplicate acceptance criterion IDs: {', '.join(duplicate_ids)}")

    for area in spec.affected_areas:
        path = area.path
        if path.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", path) or ".." in re.split(r"[\\/]", path):
            result.errors.append(f"{path} is not a repository-relative path")
        elif existing_paths is not None and _normalize_path(path) not in existing_paths:
            result.warnings.append(f"{path} does not exist yet")
    duplicate_paths = _duplicates([_normalize_path(a.path) for a in spec.affected_areas])
    if duplicate_paths:
        result.warnings.append(f"Affected areas list a path more than once: {', '.join(duplicate_paths)}")

    size = len(spec_json(spec))
    if size > SPEC_MAX_JSON_CHARS:
        result.errors.append(f"The spec is {size} characters as JSON; the limit is {SPEC_MAX_JSON_CHARS}")

    if not spec.goals:
        result.warnings.append("The spec lists no goals")
    if not any(c.verification == "automated" for c in spec.acceptance_criteria):
        result.warnings.append("No acceptance criterion is automated, so verification can't check any of them")
    if spec.open_questions:
        n = len(spec.open_questions)
        result.warnings.append(f"The spec has {n} open question(s); answer them before approving")
    return result


def spec_json(spec: Spec) -> str:
    return json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen and value not in out:
            out.append(value)
        seen.add(value)
    return out


def _normalize_path(path: str) -> str:
    return re.sub(r"/+$", "", re.sub(r"^\./", "", path))
