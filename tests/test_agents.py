from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.workspaces import LocalWorkspaceBackend

from factory.agents.registry import AGENTS, PR_REVIEWER, TRIAGE
from factory.agents.run import UploadedSandboxRunner, run_agent
from factory.agents.shell_policy import reject_reason
from factory.agents.tools import AgentDeps, bash
from factory.agents.workspace import SandboxWorkspaceBackend
from factory.runner.protocol import ResultEvent, RunnerInput
from factory.sandbox.types import ExecResult, Sandbox


class FakeSandbox:
    id = "sandbox-1"

    def __init__(self) -> None:
        self.uploads: dict[str, bytes] = {}
        self.commands: list[str] = []

    async def exec(self, command: str, *, timeout_seconds: float | None = None, as_runner: bool = False) -> ExecResult:
        del timeout_seconds
        if "factory.runner" in command:
            assert as_runner
        self.commands.append(command)
        return ExecResult(0, "", "")

    async def upload(self, path: str, data: bytes) -> None:
        self.uploads[path] = data

    async def download(self, path: str) -> bytes:
        return self.uploads[path]

    async def close(self) -> None:
        return None


def triage_output() -> dict[str, Any]:
    return {
        "decision": "draft",
        "questions": [],
        "rationale": "ready",
        "findings": "found it",
        "relevantPaths": ["src/main.py"],
    }


def output_model(data: dict[str, Any]) -> FunctionModel:
    def respond(_messages: list[Any], info: AgentInfo) -> ModelResponse:
        output = info.output_tools[0]
        return ModelResponse(parts=[ToolCallPart(output.name, data)])

    return FunctionModel(respond)


async def test_in_task_agent_returns_second_validated_output(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    sandbox = FakeSandbox()
    result = await run_agent(
        TRIAGE,
        "triage this",
        trace_id="trace-1",
        sandbox=sandbox,
        model=output_model(triage_output()),
        workspace_factory=lambda _sandbox: LocalWorkspaceBackend(tmp_path),
    )
    assert result.model_dump(by_alias=True)["decision"] == "draft"


def test_registry_pins_tool_authority_and_placement() -> None:
    assert [(agent.id, agent.tools, agent.max_turns, agent.placement) for agent in AGENTS] == [
        ("triage", ("read", "ls", "bash"), 40, "in-task"),
        ("spec-writer", ("read", "ls", "bash"), 60, "in-task"),
        ("implementer", ("read", "ls", "bash", "edit", "write"), 150, "in-sandbox"),
        ("pr-reviewer", ("read", "ls", "bash"), 100, "in-sandbox"),
    ]
    assert reject_reason("git status", "/tmp/factory") is None
    assert reject_reason("git commit -am nope", "/tmp/factory") is not None
    assert reject_reason("curl https://example.com", "/tmp/factory") is not None


async def test_read_only_shell_rejection_is_recoverable_by_the_model() -> None:
    class Context:
        deps = AgentDeps(read_only=True)

    with pytest.raises(ModelRetry, match="read-only shell policy"):
        await bash(cast(Any, Context()), "pip install surprise")


async def test_in_sandbox_input_contains_no_model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-ant-do-not-cross-boundary"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    captured: list[RunnerInput] = []

    class Runner:
        async def run(self, sandbox: Sandbox, run_input: RunnerInput, timeout_seconds: float) -> tuple[int, str]:
            del sandbox, timeout_seconds
            captured.append(run_input)
            event = ResultEvent(
                at=datetime(2026, 11, 17, 12, tzinfo=UTC),
                output={"summary": "fine", "riskTier": "trivial", "comments": []},
                turns=1,
            )
            return 0, event.model_dump_json() + "\n"

    result = await run_agent(
        PR_REVIEWER,
        "review this",
        trace_id="trace-2",
        sandbox=FakeSandbox(),
        sandbox_runner=Runner(),
    )
    assert result.model_dump(by_alias=True)["riskTier"] == "trivial"
    assert secret not in captured[0].model_dump_json()


async def test_default_sandbox_command_and_upload_contain_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-ant-do-not-cross-boundary"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    sandbox = FakeSandbox()
    run_input = RunnerInput(
        protocol_version=3,
        trace_id="trace",
        agent_id=PR_REVIEWER.id,
        system_prompt=PR_REVIEWER.prompt,
        message="review",
        tools=PR_REVIEWER.tools,
        output_schema=PR_REVIEWER.output_type.model_json_schema(mode="validation"),
        max_turns=PR_REVIEWER.max_turns,
        timeout_ms=1_000,
        cwd="/opt/factory/repo",
        scratch_dir="/opt/factory/scratch",
    )
    await UploadedSandboxRunner().run(sandbox, run_input, 1)
    serialized = b"".join(sandbox.uploads.values()).decode()
    assert secret not in serialized
    assert secret not in "\n".join(sandbox.commands)
    assert "ANTHROPIC_API_KEY" not in serialized
    assert all("key" not in Path(path).name for path in sandbox.uploads)
    assert json.loads(serialized)["protocol_version"] == 3


async def test_default_runner_uses_detached_protocol_script_when_the_sandbox_supports_it() -> None:
    class Detached(FakeSandbox):
        async def start_detached(self, command: str, *, timeout_seconds: float) -> object:
            self.commands.append(command)
            input_path = next(path for path in self.uploads if path.endswith("/input.json"))
            run_dir = input_path.removesuffix("/input.json")
            event = ResultEvent(
                at=datetime(2026, 11, 17, 12, tzinfo=UTC),
                output={"summary": "fine", "riskTier": "trivial", "comments": []},
                turns=1,
            )
            self.uploads[f"{run_dir}.jsonl"] = (event.model_dump_json() + "\n").encode()
            self.uploads[f"{run_dir}.exit"] = b"0\n"
            return object()

        async def wait_detached(self, execution: object) -> object:
            return execution

    sandbox = Detached()
    run_input = RunnerInput(
        protocol_version=3,
        trace_id="trace",
        agent_id=PR_REVIEWER.id,
        system_prompt=PR_REVIEWER.prompt,
        message="review",
        tools=PR_REVIEWER.tools,
        output_schema=PR_REVIEWER.output_type.model_json_schema(mode="validation"),
        max_turns=PR_REVIEWER.max_turns,
        timeout_ms=1_000,
        cwd="/opt/factory/repo",
        scratch_dir="/opt/factory/scratch",
    )
    status, output = await UploadedSandboxRunner().run(sandbox, run_input, 1)
    assert status == 0
    assert '"type":"result"' in output
    assert any("run-agent.sh" in command for command in sandbox.commands)


async def test_paced_workspace_rejects_paths_outside_the_repository() -> None:
    workspace = SandboxWorkspaceBackend(FakeSandbox())
    with pytest.raises(ValueError, match="escapes"):
        await workspace.read_bytes("../secret")
    with pytest.raises(ValueError, match="escapes"):
        await workspace.write_bytes("/etc/passwd", b"x")
