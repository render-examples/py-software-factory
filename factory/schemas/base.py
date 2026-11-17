"""Base models for every cross-process input and agent output.

Agent outputs and GitHub markers use camelCase JSON: the prompts are copied verbatim from the TypeScript factory
and name those fields, and markers stay readable by either implementation. Python code uses snake_case names.
"""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel


class StrictModel(BaseModel):
    """An agent output or other untrusted object: unknown keys rejected, no type coercion, camelCase JSON."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        alias_generator=to_camel,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
    )

    @classmethod
    def parse_untrusted(cls, data: Any) -> Self:
        """Validates JSON-shaped data by its camelCase keys only, as the agent's output schema shows them."""
        return cls.model_validate(data, by_alias=True, by_name=False)


class TaskInput(BaseModel):
    """A task input. The gateway builds it; Workflows validates it again on arrival."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def issues(error: ValidationError) -> list[str]:
    """Each validation issue as `path: message`, for agent-facing and log messages."""
    return [
        f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg'].removeprefix('Value error, ')}"
        for e in error.errors()
    ]
