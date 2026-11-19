from __future__ import annotations

import asyncio
import inspect
import json
import sys
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ValidationError
from pydantic_ai import UsageLimits
from pydantic_ai.exceptions import RunTimedOut, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.workspaces import LocalWorkspaceBackend

from factory.agents.definition import AgentDefinition
from factory.agents.errors import RunnerErrorCode
from factory.agents.registry import get_agent
from factory.agents.tools import AgentDeps
from factory.runner.events import RunnerEventMapper, now, started_event, usage_from
from factory.runner.model import brokered_model
from factory.runner.protocol import (
    PROTOCOL_VERSION,
    ErrorEvent,
    ResultEvent,
    RunnerEvent,
    RunnerExit,
    RunnerInput,
    UsageEvent,
)

Write = Callable[[str], Awaitable[None] | None]


class EventWriter:
    """JSONL writer that permits exactly one terminal event.

    Lines are 7-bit ASCII so byte offsets the host counts while following the file match it exactly.
    """

    def __init__(self, write: Write) -> None:
        self._write = write
        self.terminated = False

    async def emit(self, event: RunnerEvent) -> None:
        if self.terminated:
            return
        self.terminated = event.type in {"result", "error"}
        record = event.model_dump(mode="json", exclude_none=True)
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        written = self._write(line)
        if inspect.isawaitable(written):
            await written


async def main(input_text: str, write: Write, *, model: Model | None = None) -> int:
    emitter = EventWriter(write)
    try:
        raw_object: object = json.loads(input_text)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        return await _fail(emitter, "invalid_input", f"stdin is not valid JSON: {error}", RunnerExit.INVALID_INPUT)
    raw = cast(dict[str, Any], raw_object) if isinstance(raw_object, dict) else {}
    version: object = raw.get("protocol_version")
    if version != PROTOCOL_VERSION:
        message = (
            f"this runner speaks protocol {PROTOCOL_VERSION}, but the input is protocol {version!r}: "
            "the host and runner bundle are from different versions"
        )
        return await _fail(emitter, "invalid_input", message, RunnerExit.INVALID_INPUT)
    try:
        run_input = RunnerInput.model_validate_json(input_text)
        definition = get_agent(run_input.agent_id)
        _validate_authority(run_input, definition.tools, definition.max_turns, definition.output_type)
        if run_input.tools and (run_input.cwd is None or run_input.scratch_dir is None):
            raise ValueError("cwd and scratch_dir are required when tools is non-empty")
        if run_input.cwd is None:
            raise ValueError("cwd is required")
        await asyncio.to_thread(_prepare_paths, run_input.cwd, run_input.scratch_dir)
    except (ValidationError, ValueError) as error:
        return await _fail(emitter, "invalid_input", f"invalid RunnerInput: {error}", RunnerExit.INVALID_INPUT)

    async with AsyncExitStack() as stack:
        selected_model = model
        if selected_model is None:
            selected_model = await stack.enter_async_context(brokered_model(definition.model))
        return await _run_agent(definition, run_input, emitter, selected_model)


async def _run_agent(
    definition: AgentDefinition,
    run_input: RunnerInput,
    emitter: EventWriter,
    selected_model: Model,
) -> int:
    if run_input.cwd is None:
        raise AssertionError("validated runner input has no cwd")
    agent = definition.build(model=selected_model, system_prompt=run_input.system_prompt, tools=run_input.tools)
    await emitter.emit(started_event(definition.id, definition.model))
    deps = AgentDeps(
        read_only="edit" not in run_input.tools and "write" not in run_input.tools,
        scratch_dir=run_input.scratch_dir or "",
    )
    mapper = RunnerEventMapper()
    try:
        try:
            result = await agent.run(
                run_input.message,
                deps=deps,
                usage_limits=UsageLimits(request_limit=run_input.max_turns),
                timeout=run_input.timeout_ms / 1000,
                workspace=LocalWorkspaceBackend(run_input.cwd),
                event_stream_handler=mapper.stream_handler(emitter.emit),
            )
        finally:
            for event in mapper.end_response():
                await emitter.emit(event)
        validated = definition.output_type.model_validate(result.output.model_dump(mode="json", by_alias=True))
        await emitter.emit(UsageEvent(at=now(), usage=usage_from(result.usage)))
        await emitter.emit(
            ResultEvent(
                at=now(),
                output=validated.model_dump(mode="json", by_alias=True),
                turns=result.usage.requests,
            )
        )
        return RunnerExit.OK
    except UsageLimitExceeded as error:
        return await _fail(emitter, "max_turns", str(error), RunnerExit.ERROR)
    except RunTimedOut as error:
        return await _fail(emitter, "timeout", str(error), RunnerExit.TIMEOUT)
    except (UnexpectedModelBehavior, ValidationError) as error:
        return await _fail(emitter, "invalid_submission", str(error), RunnerExit.ERROR)
    except Exception as error:
        return await _fail(emitter, "agent_error", str(error), RunnerExit.ERROR)


def _validate_authority(
    run_input: RunnerInput, tools: tuple[str, ...], max_turns: int, output_type: type[BaseModel]
) -> None:
    if run_input.tools != tools:
        raise ValueError(f"tools for {run_input.agent_id} must be {list(tools)!r}")
    if run_input.max_turns > max_turns:
        raise ValueError(f"max_turns for {run_input.agent_id} cannot exceed {max_turns}")
    expected = output_type.model_json_schema(mode="validation")
    if run_input.output_schema != expected:
        raise ValueError(f"output_schema for {run_input.agent_id} does not match its registered output")


async def _fail(emitter: EventWriter, code: RunnerErrorCode, message: str, exit_code: RunnerExit) -> int:
    await emitter.emit(ErrorEvent(at=now(), code=code, message=message))
    return int(exit_code)


def _prepare_paths(cwd: str, scratch_dir: str | None) -> None:
    if not Path(cwd).is_dir():
        raise ValueError(f"cwd {cwd} is not a directory")
    if scratch_dir:
        Path(scratch_dir).mkdir(parents=True, exist_ok=True)


def cli() -> int:
    def write(line: str) -> None:
        sys.stdout.write(line)
        sys.stdout.flush()

    return asyncio.run(main(sys.stdin.read(), write))


if __name__ == "__main__":
    raise SystemExit(cli())
