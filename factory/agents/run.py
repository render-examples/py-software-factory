from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from pydantic_ai import UsageLimits
from pydantic_ai.exceptions import RunTimedOut, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.workspaces import WorkspaceBackend

from factory.agents.definition import AgentDefinition, Placement
from factory.agents.errors import AgentError
from factory.agents.registry import get_agent
from factory.agents.tools import AgentDeps
from factory.agents.workspace import SandboxWorkspaceBackend
from factory.events import FactoryEvent, emit
from factory.runner.protocol import (
    RUNNER_EVENT_ADAPTER,
    ErrorEvent,
    ResultEvent,
    RunnerEvent,
    RunnerInput,
    UsageEvent,
)
from factory.sandbox.shell import shq
from factory.sandbox.types import APP_DIR, REPO_DIR, SCRATCH_DIR, Sandbox

DEFAULT_TIMEOUT_SECONDS = 30 * 60


class SandboxRunner(Protocol):
    async def run(self, sandbox: Sandbox, run_input: RunnerInput, timeout_seconds: float) -> tuple[int, str]: ...


class UploadedSandboxRunner:
    """Default seam: integration uploads runner.zip when opening the sandbox."""

    async def run(self, sandbox: Sandbox, run_input: RunnerInput, timeout_seconds: float) -> tuple[int, str]:
        run_dir = str(PurePosixPath(SCRATCH_DIR) / "runs" / f"agent-{uuid4().hex}")
        input_path = f"{run_dir}/input.json"
        data = run_input.model_dump_json().encode()
        await sandbox.exec(f"mkdir -m 0700 -p {shq(run_dir)}", timeout_seconds=30)
        await sandbox.upload(input_path, data)
        try:
            result = await sandbox.exec(
                f"bash {shq(f'{APP_DIR}/run-agent.sh')} {shq(run_dir)} {shq(str(int(timeout_seconds)))}",
                timeout_seconds=timeout_seconds + 30,
            )
            stdout = (await sandbox.download(f"{run_dir}.jsonl")).decode(errors="replace")
            status_text = (await sandbox.download(f"{run_dir}.exit")).decode().strip()
            if not status_text.isdigit():
                raise RuntimeError(f"runner wrote invalid exit status {status_text!r}")
            status = int(status_text)
            if status != result.exit_code:
                raise RuntimeError(f"runner exit status {status} disagrees with command exit {result.exit_code}")
            return status, stdout
        finally:
            await sandbox.exec(
                f"rm -rf -- {shq(run_dir)} {shq(f'{run_dir}.jsonl')} "
                f"{shq(f'{run_dir}.stderr')} {shq(f'{run_dir}.exit')}",
                timeout_seconds=30,
            )


WorkspaceFactory = Callable[[Sandbox], WorkspaceBackend]


async def run_agent(
    agent: AgentDefinition | str,
    message: str,
    *,
    trace_id: str,
    sandbox: Sandbox,
    placement: Placement | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    model: Model | str | None = None,
    workspace_factory: WorkspaceFactory | None = None,
    sandbox_runner: SandboxRunner | None = None,
    storage: object | None = None,
) -> BaseModel:
    """Run one registered agent and validate its output twice.

    `placement` can safely move an in-sandbox definition into the Workflow task
    when credential brokering is unavailable. It never grants additional tools.
    """
    del storage  # Reserved for the follow-up spend ledger/conversation store.
    definition = get_agent(agent) if isinstance(agent, str) else agent
    selected_placement = placement or definition.placement
    if definition.placement == "in-task" and selected_placement == "in-sandbox":
        raise ValueError(f"agent {definition.id} cannot be promoted from in-task to in-sandbox at runtime")
    if not message:
        raise ValueError("agent message must not be empty")
    try:
        if selected_placement == "in-task":
            output, usage = await _run_in_task(
                definition,
                message,
                sandbox,
                timeout_seconds,
                model,
                workspace_factory,
            )
        else:
            output, usage = await _run_in_sandbox(
                definition,
                message,
                trace_id,
                sandbox,
                timeout_seconds,
                sandbox_runner,
            )
    except AgentError:
        raise
    except UsageLimitExceeded as error:
        raise AgentError(definition.id, "max_turns", str(error)) from error
    except RunTimedOut as error:
        raise AgentError(definition.id, "timeout", str(error)) from error
    except UnexpectedModelBehavior as error:
        raise AgentError(definition.id, "invalid_submission", str(error)) from error
    except ValidationError as error:
        raise AgentError(definition.id, "output_invalid", str(error)) from error
    except Exception as error:
        raise AgentError(definition.id, "agent_error", str(error)) from error
    emit(
        FactoryEvent(
            trace_id=trace_id,
            type="agent.finished",
            payload={"agent_id": definition.id, "placement": selected_placement, "usage": usage},
        )
    )
    return output


async def _run_in_task(
    definition: AgentDefinition,
    message: str,
    sandbox: Sandbox,
    timeout_seconds: float,
    model: Model | str | None,
    workspace_factory: WorkspaceFactory | None,
) -> tuple[BaseModel, dict[str, Any]]:
    workspace = workspace_factory(sandbox) if workspace_factory else SandboxWorkspaceBackend(sandbox)
    built = definition.build(model=model)
    deps = AgentDeps(read_only="edit" not in definition.tools and "write" not in definition.tools, as_runner=True)
    result = await built.run(
        message,
        deps=deps,
        usage_limits=UsageLimits(request_limit=definition.max_turns),
        timeout=timeout_seconds,
        workspace=workspace,
    )
    output = definition.output_type.model_validate(result.output.model_dump(mode="json", by_alias=True))
    usage = result.usage
    return output, {
        "requests": usage.requests,
        "tool_calls": usage.tool_calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": float(usage.cost) if usage.cost is not None else None,
    }


async def _run_in_sandbox(
    definition: AgentDefinition,
    message: str,
    trace_id: str,
    sandbox: Sandbox,
    timeout_seconds: float,
    sandbox_runner: SandboxRunner | None,
) -> tuple[BaseModel, dict[str, Any]]:
    run_input = RunnerInput(
        protocol_version=3,
        trace_id=trace_id,
        agent_id=definition.id,
        system_prompt=definition.prompt,
        message=message,
        tools=definition.tools,
        output_schema=definition.output_type.model_json_schema(mode="validation"),
        max_turns=definition.max_turns,
        timeout_ms=int(timeout_seconds * 1000),
        cwd=REPO_DIR,
        scratch_dir=SCRATCH_DIR,
    )
    exit_code, stdout = await (sandbox_runner or UploadedSandboxRunner()).run(sandbox, run_input, timeout_seconds)
    events = _parse_events(definition.id, stdout)
    terminals = [event for event in events if isinstance(event, (ResultEvent, ErrorEvent))]
    if len(terminals) != 1 or events[-1] is not terminals[0]:
        raise AgentError(definition.id, "runner_lost", "runner stream must end in exactly one terminal event")
    terminal = terminals[0]
    if isinstance(terminal, ErrorEvent):
        raise AgentError(definition.id, terminal.code, terminal.message)
    if exit_code != 0:
        raise AgentError(definition.id, "runner_lost", f"runner returned exit code {exit_code} after a result")
    try:
        output: BaseModel = definition.output_type.model_validate(terminal.output, by_alias=True, by_name=False)
    except ValidationError as error:
        raise AgentError(definition.id, "output_invalid", str(error)) from error
    usage_event = next((event for event in reversed(events) if isinstance(event, UsageEvent)), None)
    usage = usage_event.usage.model_dump() if usage_event else {}
    return output, usage


def _parse_events(agent_id: str, stdout: str) -> list[RunnerEvent]:
    events: list[RunnerEvent] = []
    for line_number, line in enumerate(stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(RUNNER_EVENT_ADAPTER.validate_json(line))
        except ValidationError as error:
            raise AgentError(agent_id, "runner_lost", f"invalid runner event on line {line_number}: {error}") from error
    if not events:
        raise AgentError(agent_id, "runner_lost", "runner produced no events")
    return events
