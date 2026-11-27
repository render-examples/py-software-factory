from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel


class StrictModel(BaseModel):
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
        return cls.model_validate(data, by_alias=True, by_name=False)


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def issues(error: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg'].removeprefix('Value error, ')}"
        for e in error.errors()
    ]
