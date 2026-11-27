"""Hidden markers on factory-written comments: `<!-- software-factory:<kind> <base64url JSON> -->`.

They're how a run finds its own earlier writes in GitHub, so they're trusted only on comments the App's bot
authored. The JSON matches the TypeScript factory's, so either implementation reads the other's comments.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from collections.abc import Sequence
from typing import Annotated, Any, ClassVar, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.alias_generators import to_camel

from factory.github.types import IssueComment

RunState = Literal["running", "done", "failed", "cancelled", "rejected"]

_MAX_MARKER_CHARS = 48_000
"""GitHub caps a comment at 65,536 characters; the marker leaves room for the readable part."""


class Marker(BaseModel):
    """Unknown keys are dropped, not rejected, so a marker written by a newer version still reads."""

    kind: ClassVar[str]
    model_config = ConfigDict(
        extra="ignore",
        strict=True,
        alias_generator=to_camel,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
        frozen=True,
    )


class StatusMarker(Marker):
    kind: ClassVar[str] = "status"
    """Shown for people and debugging. Run state lives in Postgres; nothing parses this to decide anything."""

    run_id: str = Field(min_length=1)
    trace_id: str | None = Field(default=None, min_length=1)
    """Root Workflows run. Differs from run_id when this run is a subtask."""
    stage: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1)
    """The run's name as the comment shows it."""
    state: RunState


class SpecMarker(Marker):
    kind: ClassVar[str] = "spec"
    spec: Any
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    """sha256 of the spec's canonical JSON; a marker whose spec doesn't match it is ignored."""
    version: int = Field(gt=0)
    notes: Any = None
    """The spec writer's notes, kept for later rounds. Not covered by `hash`."""

    @model_validator(mode="after")
    def _hash_matches(self) -> Self:
        if self.hash != content_hash(self.spec):
            raise ValueError("spec does not match its hash")
        return self


class QuestionsMarker(Marker):
    kind: ClassVar[str] = "questions"
    round: int = Field(gt=0)
    questions: list[str]


class ReviewMarker(Marker):
    kind: ClassVar[str] = "review"
    head_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    run_id: str = Field(min_length=1)


def render_marker(data: Marker) -> str:
    payload = json.dumps(data.model_dump(mode="json", exclude_none=True), ensure_ascii=False, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    marker = f"<!-- software-factory:{data.kind} {encoded} -->"
    if len(marker) > _MAX_MARKER_CHARS:
        raise ValueError(f"{data.kind} marker is {len(marker)} characters; the limit is {_MAX_MARKER_CHARS}")
    return marker


M = TypeVar("M", bound=Marker)


def parse_marker(body: str, cls: type[M]) -> M | None:
    """The marker's data, or None when the body has none or it doesn't decode and validate. Tolerates surrounding
    edits, whitespace, and CRLF; with several markers of one kind, the last one wins."""
    pattern = re.compile(rf"<!--\s*software-factory:{re.escape(cls.kind)}\s+([A-Za-z0-9_-]+)\s*-->")
    matches = pattern.findall(body)
    if not matches:
        return None
    encoded: str = matches[-1]
    try:
        data = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        return cls.model_validate(data, by_alias=True, by_name=False)
    except (binascii.Error, UnicodeDecodeError, ValueError, ValidationError):
        return None


def find_marked_comment(
    comments: Sequence[IssueComment], cls: type[M], bot_login: str
) -> tuple[IssueComment, M] | None:
    """The newest comment carrying a valid marker of this kind and authored by the App's bot. Anyone can paste a
    marker."""
    bot = bot_login.lower()
    for comment in reversed(comments):
        if comment.author.login.lower() != bot:
            continue
        data = parse_marker(comment.body, cls)
        if data is not None:
            return comment, data
    return None


def content_hash(value: Any) -> str:
    """sha256 of the value's canonical JSON (object keys sorted), so key order never changes the hash."""
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def canonical_json(value: Any) -> str:
    """JSON with sorted keys and no whitespace, written as JavaScript's JSON.stringify writes it."""
    if isinstance(value, dict):
        obj: dict[Any, Any] = value  # pyright: ignore[reportUnknownVariableType]
        keys = sorted(str(k) for k in obj)
        return "{" + ",".join(f"{_string(k)}:{canonical_json(obj[k])}" for k in keys) + "}"
    if isinstance(value, list | tuple):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return "null"
        return str(int(value)) if value.is_integer() and abs(value) < 1e21 else repr(value)
    if isinstance(value, str):
        return _string(value)
    raise TypeError(f"cannot hash a {type(value).__name__}")


def _string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
