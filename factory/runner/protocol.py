from __future__ import annotations

from datetime import datetime
from enum import IntEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from factory.agents.errors import RunnerErrorCode
from factory.agents.tools import ToolName

PROTOCOL_VERSION = 3


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RunnerInput(ProtocolModel):
    protocol_version: Literal[3]
    trace_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    message: str = Field(min_length=1)
    tools: tuple[ToolName, ...]
    output_schema: dict[str, Any]
    max_turns: int = Field(gt=0)
    timeout_ms: int = Field(gt=0)
    cwd: str | None = Field(default=None, min_length=1)
    scratch_dir: str | None = Field(default=None, min_length=1)


class Usage(ProtocolModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class Event(ProtocolModel):
    at: datetime


class StartedEvent(Event):
    type: Literal["started"] = "started"
    agent_id: str
    harness: str
    harness_version: str
    model: str


class ToolCallEvent(Event):
    type: Literal["tool_call"] = "tool_call"
    tool_call_id: str
    tool: str
    args: Any


class ToolResultEvent(Event):
    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    tool: str
    is_error: bool
    output_preview: str


class AssistantTextEvent(Event):
    type: Literal["assistant_text"] = "assistant_text"
    text: str


class UsageEvent(Event):
    type: Literal["usage"] = "usage"
    usage: Usage


class ResultEvent(Event):
    type: Literal["result"] = "result"
    output: Any
    turns: int = Field(ge=0)


class ErrorEvent(Event):
    type: Literal["error"] = "error"
    code: RunnerErrorCode
    message: str


RunnerEvent = Annotated[
    StartedEvent | ToolCallEvent | ToolResultEvent | AssistantTextEvent | UsageEvent | ResultEvent | ErrorEvent,
    Field(discriminator="type"),
]
RUNNER_EVENT_ADAPTER = TypeAdapter(RunnerEvent)


class RunnerExit(IntEnum):
    OK = 0
    ERROR = 1
    INVALID_INPUT = 2
    TIMEOUT = 3
