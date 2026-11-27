"""Agents end to end: the real Pydantic AI loop, Harness capabilities, and sandbox/fileops.py, with a scripted model and
a sandbox that runs commands locally."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.anthropic import AnthropicModelName
from pydantic_ai.models.function import AgentInfo, FunctionModel

from factory.agents.errors import AgentError
from factory.agents.files import FILEOPS, FileOpRefused, SandboxFiles
from factory.agents.registry import AGENTS, IMPLEMENTER, PR_REVIEWER, SPEC_WRITER, TRIAGE
from factory.agents.run import run_agent
from factory.agents.workspace import (
    DEFAULT_COMMAND_SECONDS,
    KILL_GRACE_SECONDS,
    MAX_COMMAND_SECONDS,
    SandboxWorkspace,
)
from factory.events import EventIds
from factory.sandbox import APP_DIR, REPO_DIR, RUN_AS, SANDBOX_PYTHON, SCRATCH_DIR, ExecResult, shq

ROOT = Path(__file__).resolve().parents[1]
EVENT_IDS = EventIds("trace-1", "run-1", "acme/repo", "issue#1")
AS_RUNNER = f"bash {shq(f'{APP_DIR}/as-runner.sh')} "
TIMEOUT_SHIM = """#!PYTHON
import subprocess, sys
args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
try:
    sys.exit(subprocess.run(args[1:], timeout=float(args[0])).returncode)
except subprocess.TimeoutExpired:
    sys.exit(124)
"""


class LocalSandbox:
    """Runs commands with bash in a local checkout that stands in for REPO_DIR, using this repo's fileops.py."""

    id = "sandbox-1"

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.scratch = repo.parent / f"{repo.name}-scratch"
        self.calls: list[tuple[str, float | None, bool]] = []
        self.uploads: dict[str, bytes] = {}
        self.path = os.environ["PATH"]
        if shutil.which("timeout") is None:
            # The sandbox has coreutils; this machine may not. Same arguments and exit 124 on expiry.
            tools = self.scratch / "bin"
            tools.mkdir(parents=True, exist_ok=True)
            (tools / "timeout").write_text(TIMEOUT_SHIM.replace("PYTHON", sys.executable))
            (tools / "timeout").chmod(0o755)
            self.path = f"{tools}{os.pathsep}{self.path}"

    def _local(self, command: str) -> str:
        local = (
            command.replace(SANDBOX_PYTHON, shq(sys.executable))
            .replace(FILEOPS, str(ROOT / "sandbox" / "fileops.py"))
            .replace(REPO_DIR, str(self.repo))
            .replace(SCRATCH_DIR, str(self.scratch))
            .replace(AS_RUNNER, "")
        )
        # There is no runner user locally to hand a staged request to.
        return re.sub(rf"^chown {re.escape(shq(RUN_AS))}: \S+ && ", "", local)

    async def exec(self, command: str, *, timeout_seconds: float | None = None, as_runner: bool = False) -> ExecResult:
        self.calls.append((command, timeout_seconds, as_runner))
        result = subprocess.run(
            ["bash", "-c", self._local(command)],
            cwd=self.repo,
            env={**os.environ, "PATH": self.path},
            capture_output=True,
            text=True,
            check=False,
        )
        return ExecResult(result.returncode, result.stdout, result.stderr)

    async def upload(self, path: str, data: bytes) -> None:
        self.uploads[path] = data
        local = Path(self._local(path))
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)

    async def download(self, path: str) -> bytes:
        raise AssertionError("agents never download files")

    async def close(self) -> None:
        return None


Step = Callable[[list[ModelMessage], AgentInfo], ModelResponse]


def scripted(*calls: tuple[str, dict[str, Any]]) -> tuple[FunctionModel, list[str]]:
    """A model that makes `calls` in order, then submits; returns it and the tool results it saw."""
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        if isinstance(last, ModelRequest):
            seen.extend(str(part.content) for part in last.parts if isinstance(part, ToolReturnPart))
        step = sum(isinstance(message, ModelResponse) for message in messages)
        if step < len(calls):
            name, args = calls[step]
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, OUTPUTS[info.output_tools[0].name])])

    return FunctionModel(respond), seen


OUTPUTS: dict[str, dict[str, Any]] = {}


def submits(output: dict[str, Any]) -> None:
    OUTPUTS["submit"] = output


TRIAGE_OUTPUT = {
    "decision": "draft",
    "questions": [],
    "rationale": "ready",
    "findings": "found it",
    "relevantPaths": ["src/app.py"],
}
IMPLEMENTATION = {
    "summary": "changed it",
    "filesChanged": ["src/app.py"],
    "verification": [],
    "followUps": [],
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def greet():\n    return 'hello'\n")
    return tmp_path


def test_registry_uses_explicit_supported_anthropic_models() -> None:
    literal = next(item for item in get_args(AnthropicModelName) if get_origin(item) is Literal)
    configured = {agent.model.partition(":")[2] for agent in AGENTS}
    assert configured == {"claude-sonnet-5-5", "claude-opus-5-5"}
    assert configured <= set(get_args(literal))


def test_only_the_implementer_writes() -> None:
    assert [agent.id for agent in AGENTS if agent.writes] == [IMPLEMENTER.id]


@pytest.mark.parametrize(
    ("agent", "tools"),
    [
        (TRIAGE, {"read_file", "list_files", "grep", "run_command"}),
        (SPEC_WRITER, {"read_file", "list_files", "grep", "run_command"}),
        (PR_REVIEWER, {"read_file", "list_files", "grep", "run_command"}),
        (IMPLEMENTER, {"read_file", "list_files", "grep", "run_command", "edit_file", "write_file"}),
    ],
)
async def test_each_agent_gets_exactly_its_tools(agent: Any, tools: set[str], repo: Path) -> None:
    offered: list[set[str]] = []

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.append({tool.name for tool in info.function_tools})
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        await run_agent(agent, "go", event_ids=EVENT_IDS, sandbox=LocalSandbox(repo), model=FunctionModel(respond))
    assert offered == [tools]


async def test_run_returns_validated_output_and_emits_usage(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    submits(TRIAGE_OUTPUT)
    model, _ = scripted()
    result = await run_agent(TRIAGE, "triage this", event_ids=EVENT_IDS, sandbox=LocalSandbox(repo), model=model)
    assert result.model_dump(by_alias=True)["decision"] == "draft"
    event = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert {key: event[key] for key in ("event", "trace_id", "run_id", "repo", "subject", "agent_id", "requests")} == {
        "event": "agent.finished",
        "trace_id": "trace-1",
        "run_id": "run-1",
        "repo": "acme/repo",
        "subject": "issue#1",
        "agent_id": "triage",
        "requests": 1,
    }


async def test_implementer_edits_files_inside_the_sandbox(repo: Path) -> None:
    submits(IMPLEMENTATION)
    model, seen = scripted(
        ("read_file", {"path": "src/app.py"}),
        ("edit_file", {"path": "src/app.py", "edits": [{"old_text": "'hello'", "new_text": "'hi'"}]}),
        ("write_file", {"path": "src/new/module.py", "content": "VALUE = 1\n"}),
        ("grep", {"pattern": "hi", "path": "src"}),
    )
    sandbox = LocalSandbox(repo)
    await run_agent(IMPLEMENTER, "implement", event_ids=EVENT_IDS, sandbox=sandbox, model=model)

    assert (repo / "src" / "app.py").read_text() == "def greet():\n    return 'hi'\n"
    assert (repo / "src" / "new" / "module.py").read_text() == "VALUE = 1\n"
    assert "     2\t    return 'hello'" in seen[0]
    assert seen[1] == "Edited src/app.py (1 replacement)."
    assert seen[2] == "Created src/new/module.py (10 bytes)."
    if shutil.which("rg"):
        assert "src/app.py:2:    return 'hi'" in seen[3]
    # Every file operation is one command, run as the runner user; no file was downloaded.
    file_calls = [call for call in sandbox.calls if FILEOPS in call[0]]
    assert len(file_calls) == 4
    assert all(as_runner for _, _, as_runner in file_calls)


async def test_a_refused_edit_is_reported_to_the_model_and_changes_nothing(repo: Path) -> None:
    submits(IMPLEMENTATION)
    model, seen = scripted(
        (
            "edit_file",
            {
                "path": "src/app.py",
                "edits": [{"old_text": "def", "new_text": "async def"}, {"old_text": "nope", "new_text": "x"}],
            },
        ),
        ("write_file", {"path": "../escape.txt", "content": "x"}),
    )
    await run_agent(IMPLEMENTER, "implement", event_ids=EVENT_IDS, sandbox=LocalSandbox(repo), model=model)
    assert seen[0] == "Error: edit 2: old_text was not found in src/app.py. No edits were applied."
    assert seen[1] == "Error: ../escape.txt is outside the repository"
    assert (repo / "src" / "app.py").read_text().startswith("def greet")
    assert not (repo.parent / "escape.txt").exists()


async def test_read_only_agents_cannot_redirect_or_run_unlisted_commands(repo: Path) -> None:
    submits(TRIAGE_OUTPUT)
    model, seen = scripted(
        ("run_command", {"command": "echo hi > src/written.txt"}),
        ("run_command", {"command": 'python3 -c \'open("src/written.txt", "w")\''}),
        ("run_command", {"command": "git --version"}),
    )
    await run_agent(TRIAGE, "triage", event_ids=EVENT_IDS, sandbox=LocalSandbox(repo), model=model)
    assert not (repo / "src" / "written.txt").exists()
    assert "git version" in seen[-1]


async def test_turn_limit_maps_to_max_turns(repo: Path) -> None:
    def loop(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("list_files", {"path": "src"})])

    limited = replace(TRIAGE, max_turns=2)
    with pytest.raises(AgentError) as raised:
        await run_agent(limited, "go", event_ids=EVENT_IDS, sandbox=LocalSandbox(repo), model=FunctionModel(loop))
    assert raised.value.code == "max_turns"


async def test_workspace_runs_commands_as_the_runner_with_bounded_timeouts(repo: Path) -> None:
    sandbox = LocalSandbox(repo)
    workspace = SandboxWorkspace(sandbox)
    assert await workspace.working_dir() == REPO_DIR
    result = await workspace.run(  # noqa: S604 - the sandbox shell under test
        "echo $GREETING",
        shell=True,
        env={"GREETING": "hello world"},
        timeout=99_999,
    )
    assert result.stdout == "hello world\n"
    await workspace.run(["printf", "%s", "a b"])
    assert [(timeout, as_runner) for _, timeout, as_runner in sandbox.calls] == [
        (MAX_COMMAND_SECONDS + KILL_GRACE_SECONDS, True),
        (DEFAULT_COMMAND_SECONDS + KILL_GRACE_SECONDS, True),
    ]
    assert shlex.split(sandbox.calls[1][0]) == [
        "timeout",
        f"--kill-after={KILL_GRACE_SECONDS}s",
        str(DEFAULT_COMMAND_SECONDS),
        "bash",
        "-c",
        "'printf' '%s' 'a b'",
    ]


async def test_a_command_that_runs_over_its_limit_reports_exit_124(repo: Path) -> None:
    result = await SandboxWorkspace(LocalSandbox(repo)).run(["sleep", "5"], timeout=0.2)
    assert result.exit_code == 124


async def test_large_requests_are_uploaded_instead_of_passed_as_an_argument(repo: Path) -> None:
    sandbox = LocalSandbox(repo)
    files = SandboxFiles(sandbox, str(repo))
    content = "x = '''" + "q'" * 70_000 + "'''\n"
    result = await files.request("write", path="big.py", content=content)
    assert result["bytes"] == len(content)
    assert (repo / "big.py").read_text() == content
    [staged] = sandbox.uploads
    assert staged.endswith(".json")
    command, _, as_runner = sandbox.calls[-1]
    assert not as_runner
    assert command.startswith("chown ")
    assert "--request-file" in command
    assert not Path(sandbox._local(staged)).exists()


async def test_small_requests_travel_inline(repo: Path) -> None:
    sandbox = LocalSandbox(repo)
    await SandboxFiles(sandbox, str(repo)).request("read", path="src/app.py")
    assert sandbox.uploads == {}
    assert len(sandbox.calls) == 1


async def test_refusals_and_failures_are_distinguished(repo: Path) -> None:
    files = SandboxFiles(LocalSandbox(repo), str(repo))
    with pytest.raises(FileOpRefused, match="does not exist"):
        await files.request("read", path="missing.py")
    with pytest.raises(RuntimeError, match="malformed request"):
        await files.request("unknown-op")
