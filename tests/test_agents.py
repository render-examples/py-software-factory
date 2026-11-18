from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast, get_args, get_origin

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.anthropic import AnthropicModelName
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.workspaces import LocalWorkspaceBackend, Workspace

from factory.agents.registry import AGENTS, PR_REVIEWER, TRIAGE
from factory.agents.run import UploadedSandboxRunner, run_agent
from factory.agents.shell_policy import reject_reason
from factory.agents.tools import AgentDeps, _shell_env, bash, edit, ls, read, write
from factory.agents.workspace import SandboxWorkspaceBackend
from factory.runner.protocol import ResultEvent, RunnerInput
from factory.sandbox.types import (
    APP_DIR,
    REPO_DIR,
    RUNNER_HOME,
    RUNNER_PATH,
    RUNNER_TMP,
    ExecResult,
    Sandbox,
)


class FakeSandbox:
    id = "sandbox-1"

    def __init__(self) -> None:
        self.uploads: dict[str, bytes] = {}
        self.commands: list[str] = []
        self.runner_output = b""

    async def exec(self, command: str, *, timeout_seconds: float | None = None, as_runner: bool = False) -> ExecResult:
        del timeout_seconds, as_runner
        self.commands.append(command)
        if "run-agent.sh" in command:
            input_path = next(path for path in self.uploads if path.endswith("/input.json"))
            run_dir = input_path.removesuffix("/input.json")
            self.uploads[f"{run_dir}.jsonl"] = self.runner_output
            self.uploads[f"{run_dir}.exit"] = b"0\n"
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
        assert output.name == "submit"
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


async def test_in_sandbox_definition_can_safely_fall_back_to_task_placement(tmp_path: Path) -> None:
    result = await run_agent(
        PR_REVIEWER,
        "review this",
        trace_id="trace-fallback",
        sandbox=FakeSandbox(),
        placement="in-task",
        model=output_model({"summary": "fine", "riskTier": "trivial", "comments": []}),
        workspace_factory=lambda _sandbox: LocalWorkspaceBackend(tmp_path),
    )
    assert result.model_dump(by_alias=True)["riskTier"] == "trivial"


async def test_in_task_definition_cannot_be_promoted_to_sandbox() -> None:
    with pytest.raises(ValueError, match="cannot be promoted"):
        await run_agent(
            TRIAGE,
            "triage this",
            trace_id="trace-unsafe",
            sandbox=FakeSandbox(),
            placement="in-sandbox",
        )


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


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("env -u FOO rm -rf .", "rm"),
        ("xargs -I {} curl https://example.com", "curl"),
        ("git --git-dir .git clean -fd", "git clean"),
        ("git --work-tree . reset --hard", "git reset"),
        ("echo hi >|tracked.txt", "redirecting output"),
        ("time -o report.txt echo hi", "time output"),
    ],
)
def test_read_only_shell_policy_rejects_wrapper_and_redirect_bypasses(command: str, reason: str) -> None:
    assert reason in (reject_reason(command, "/opt/factory/scratch") or "")


@pytest.mark.parametrize(
    "command",
    [
        "env -u FOO git status",
        "xargs -I {} git status",
        "git --git-dir .git --work-tree . status",
        "echo hi >|/tmp/report.txt",
    ],
)
def test_read_only_shell_policy_keeps_safe_wrapped_commands(command: str) -> None:
    assert reject_reason(command, "/opt/factory/scratch") is None


def test_registry_uses_explicit_supported_anthropic_models() -> None:
    literal = next(item for item in get_args(AnthropicModelName) if get_origin(item) is Literal)
    supported = set(get_args(literal))
    configured = {agent.model.partition(":")[2] for agent in AGENTS}
    assert configured == {"claude-sonnet-5-5", "claude-opus-5-5"}
    assert configured <= supported


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
    serialized = next(data for path, data in sandbox.uploads.items() if path.endswith("/input.json")).decode()
    assert secret not in serialized
    assert secret not in "\n".join(sandbox.commands)
    assert "ANTHROPIC_API_KEY" not in serialized
    assert all("key" not in Path(path).name for path in sandbox.uploads)
    assert json.loads(serialized)["protocol_version"] == 3


async def test_default_runner_uses_protocol_script_through_standard_exec() -> None:
    sandbox = FakeSandbox()
    event = ResultEvent(
        at=datetime(2026, 11, 17, 12, tzinfo=UTC),
        output={"summary": "fine", "riskTier": "trivial", "comments": []},
        turns=1,
    )
    sandbox.runner_output = (event.model_dump_json() + "\n").encode()
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


async def test_sandbox_workspace_operations_always_run_as_runner() -> None:
    class RecordingSandbox(FakeSandbox):
        def __init__(self) -> None:
            super().__init__()
            self.runner_flags: list[bool] = []

        async def exec(
            self, command: str, *, timeout_seconds: float | None = None, as_runner: bool = False
        ) -> ExecResult:
            del timeout_seconds
            self.commands.append(command)
            self.runner_flags.append(as_runner)
            stdout = base64.b64encode(b"hello").decode() if "b64encode" in command else ""
            return ExecResult(0, stdout, "")

    sandbox = RecordingSandbox()
    workspace = SandboxWorkspaceBackend(sandbox)
    assert await workspace.read_bytes(f"{REPO_DIR}/file.txt") == b"hello"
    await workspace.write_bytes(f"{REPO_DIR}/file.txt", b"changed")
    await workspace.run(["git", "status"], timeout=30)
    assert sandbox.runner_flags == [True, True, True]


async def test_file_tools_share_workspace_operations_and_reject_escape_paths(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "notes.txt").write_text("before")
    (tmp_path / "outside-link").symlink_to(outside)

    class Context:
        deps = AgentDeps(read_only=False)
        workspace = Workspace(LocalWorkspaceBackend(tmp_path))

    ctx = cast(Any, Context())
    assert await read(ctx, "notes.txt") == "before"
    assert "notes.txt" in await ls(ctx)
    assert await edit(ctx, "notes.txt", "before", "after") == "edited notes.txt"
    assert await write(ctx, "nested/new.txt", "new") == "wrote nested/new.txt"
    assert (tmp_path / "notes.txt").read_text() == "after"
    assert (tmp_path / "nested/new.txt").read_text() == "new"

    for path in ("../outside.txt", str(outside), "outside-link"):
        with pytest.raises(ModelRetry, match="escapes"):
            await read(ctx, path)


def test_shell_environment_matches_as_runner_layout() -> None:
    environment = _shell_env()
    assert environment["HOME"] == RUNNER_HOME
    assert environment["TMPDIR"] == RUNNER_TMP
    assert environment["PATH"] == RUNNER_PATH
    assert environment["GIT_CONFIG_GLOBAL"] == f"{APP_DIR}/gitconfig"
