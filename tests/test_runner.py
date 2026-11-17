from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import pydantic_ai
import pytest
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from factory.agents.registry import TRIAGE
from factory.runner.main import main
from factory.runner.model import _strip_anthropic_key, brokered_model
from factory.runner.protocol import (
    RUNNER_EVENT_ADAPTER,
    ErrorEvent,
    ResultEvent,
    RunnerEvent,
    RunnerInput,
    StartedEvent,
)


def runner_input(tmp_path: Path, *, max_turns: int = 40) -> RunnerInput:
    scratch = tmp_path / "scratch"
    return RunnerInput(
        protocol_version=3,
        trace_id="trace-1",
        agent_id=TRIAGE.id,
        system_prompt=TRIAGE.prompt,
        message="triage this",
        tools=TRIAGE.tools,
        output_schema=TRIAGE.output_type.model_json_schema(mode="validation"),
        max_turns=max_turns,
        timeout_ms=5_000,
        cwd=str(tmp_path),
        scratch_dir=str(scratch),
    )


def parse_events(lines: list[str]) -> list[RunnerEvent]:
    return [RUNNER_EVENT_ADAPTER.validate_json(line) for line in lines]


async def test_runner_valid_output_and_terminal_event(tmp_path: Path) -> None:
    async def respond(_messages: list[Any], info: AgentInfo):  # type annotation inferred by FunctionModel
        output = info.output_tools[0]
        assert output.name == "submit"
        args: dict[str, Any] = {
            "decision": "draft",
            "questions": [],
            "rationale": "ready",
            "findings": "repository inspected",
            "relevantPaths": [],
        }
        yield {
            0: DeltaToolCall(
                name=output.name,
                json_args=json.dumps(args),
                tool_call_id="result-1",
            )
        }

    lines: list[str] = []
    code = await main(
        runner_input(tmp_path).model_dump_json(),
        lines.append,
        model=FunctionModel(stream_function=respond),
    )
    events = parse_events(lines)
    assert code == 0
    assert isinstance(events[-1], ResultEvent)
    assert sum(isinstance(event, (ResultEvent, ErrorEvent)) for event in events) == 1
    assert any(event.type == "usage" for event in events)
    started = next(event for event in events if isinstance(event, StartedEvent))
    assert started.harness_version == pydantic_ai.__version__


async def test_runner_enforces_request_limit_and_emits_one_error(tmp_path: Path) -> None:
    async def keep_calling(_messages: list[Any], info: AgentInfo):  # type annotation inferred by FunctionModel
        bash = next(tool for tool in info.function_tools if tool.name == "bash")
        yield {
            0: DeltaToolCall(
                name=bash.name,
                json_args=json.dumps({"command": "pwd"}),
                tool_call_id="bash-1",
            )
        }

    lines: list[str] = []
    code = await main(
        runner_input(tmp_path, max_turns=1).model_dump_json(),
        lines.append,
        model=FunctionModel(stream_function=keep_calling),
    )
    events = parse_events(lines)
    assert code == 1
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].code == "max_turns"
    assert sum(isinstance(event, (ResultEvent, ErrorEvent)) for event in events) == 1


async def test_runner_invalid_input_has_protocol_exit_code() -> None:
    lines: list[str] = []
    code = await main(json.dumps({"protocol_version": 2}), lines.append)
    events = parse_events(lines)
    assert code == 2
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "invalid_input"


async def test_runner_maps_invalid_submission(tmp_path: Path) -> None:
    async def text_only(_messages: list[Any], _info: AgentInfo):  # type annotation inferred by FunctionModel
        yield "not structured"

    lines: list[str] = []
    code = await main(
        runner_input(tmp_path).model_dump_json(),
        lines.append,
        model=FunctionModel(stream_function=text_only),
    )
    events = parse_events(lines)
    assert code == 1
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].code == "invalid_submission"


async def test_runner_timeout_has_timeout_exit_code(tmp_path: Path) -> None:
    async def slow(_messages: list[Any], _info: AgentInfo):  # type annotation inferred by FunctionModel
        await asyncio.sleep(1)
        yield "too late"

    run_input = runner_input(tmp_path)
    run_input.timeout_ms = 1
    lines: list[str] = []
    code = await main(
        run_input.model_dump_json(),
        lines.append,
        model=FunctionModel(stream_function=slow),
    )
    events = parse_events(lines)
    assert code == 3
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].code == "timeout"


@pytest.mark.parametrize(("valid", "expected_code"), [(True, 0), (False, 1)])
async def test_runner_exits_owned_broker_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    valid: bool,
    expected_code: int,
) -> None:
    async def respond(_messages: list[Any], info: AgentInfo):  # type annotation inferred by FunctionModel
        if not valid:
            yield "not structured"
            return
        output = info.output_tools[0]
        yield {
            0: DeltaToolCall(
                name=output.name,
                json_args=json.dumps(
                    {
                        "decision": "draft",
                        "questions": [],
                        "rationale": "ready",
                        "findings": "inspected",
                        "relevantPaths": [],
                    }
                ),
                tool_call_id="result-owned",
            )
        }

    exited = False

    @asynccontextmanager
    async def owned_model(_model_name: str) -> AsyncGenerator[Model]:
        nonlocal exited
        try:
            yield FunctionModel(stream_function=respond)
        finally:
            exited = True

    runner_module = importlib.import_module("factory.runner.main")
    monkeypatch.setattr(runner_module, "brokered_model", owned_model)
    lines: list[str] = []
    code = await runner_module.main(runner_input(tmp_path).model_dump_json(), lines.append)
    assert code == expected_code, lines
    assert exited


async def test_runner_does_not_enter_broker_context_for_injected_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def unexpected(_model_name: str) -> AsyncGenerator[Model]:
        raise AssertionError("injected models are caller-owned")
        yield FunctionModel()  # pragma: no cover

    async def respond(_messages: list[Any], info: AgentInfo):  # type annotation inferred by FunctionModel
        output = info.output_tools[0]
        yield {
            0: DeltaToolCall(
                name=output.name,
                json_args=json.dumps(
                    {
                        "decision": "draft",
                        "questions": [],
                        "rationale": "ready",
                        "findings": "",
                        "relevantPaths": [],
                    }
                ),
                tool_call_id="result-injected",
            )
        }

    runner_module = importlib.import_module("factory.runner.main")
    monkeypatch.setattr(runner_module, "brokered_model", unexpected)
    lines: list[str] = []
    code = await runner_module.main(
        runner_input(tmp_path).model_dump_json(),
        lines.append,
        model=FunctionModel(stream_function=respond),
    )
    assert code == 0, lines


async def test_brokered_model_closes_its_http_client() -> None:
    async with brokered_model(TRIAGE.model) as model:
        client = model._provider._client._client
        assert not client.is_closed
    assert client.is_closed


async def test_broker_hook_removes_client_authentication_header() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages", headers={"x-api-key": "placeholder"})
    await _strip_anthropic_key(request)
    assert "x-api-key" not in request.headers
