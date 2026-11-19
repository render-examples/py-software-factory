from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from pydantic_ai import UsageLimits
from pydantic_ai.exceptions import RunTimedOut, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.workspaces import WorkspaceBackend

from factory.agent_runs.relay import LineRelay, decode
from factory.agents.definition import AgentDefinition, Placement
from factory.agents.errors import AgentError, AgentErrorCode, RunnerErrorCode
from factory.agents.registry import get_agent
from factory.agents.tools import AgentDeps
from factory.agents.workspace import SandboxWorkspaceBackend
from factory.events import EventIds, FactoryEvent, emit, warn
from factory.runner.events import RunnerEventMapper, now, started_event, usage_from
from factory.runner.protocol import (
    RUNNER_EVENT_ADAPTER,
    ErrorEvent,
    ResultEvent,
    RunnerEvent,
    RunnerInput,
    UsageEvent,
)
from factory.sandbox.client import measure_sandbox_requests
from factory.sandbox.shell import shq
from factory.sandbox.types import APP_DIR, REPO_DIR, SCRATCH_DIR, FollowingSandbox, Sandbox

DEFAULT_TIMEOUT_SECONDS = 30 * 60

OnEvent = Callable[[RunnerEvent], None]


class SandboxRunner(Protocol):
    async def run(self, sandbox: Sandbox, run_input: RunnerInput, timeout_seconds: float) -> tuple[int, str]: ...


class UploadedSandboxRunner:
    """Default seam: integration uploads runner.zip when opening the sandbox.

    With `on_event`, the runner's events are relayed as it writes them (when the sandbox can follow files) and
    after it exits. They're for display only: the returned output is always the downloaded file.
    """

    def __init__(self, on_event: OnEvent | None = None) -> None:
        self._on_event = on_event

    async def run(self, sandbox: Sandbox, run_input: RunnerInput, timeout_seconds: float) -> tuple[int, str]:
        run_dir = str(PurePosixPath(SCRATCH_DIR) / "runs" / f"agent-{uuid4().hex}")
        input_path = f"{run_dir}/input.json"
        data = run_input.model_dump_json().encode()
        await sandbox.exec(f"mkdir -m 0700 -p {shq(run_dir)}", timeout_seconds=30)
        await sandbox.upload(input_path, data)
        try:
            command = f"bash {shq(f'{APP_DIR}/run-agent.sh')} {shq(run_dir)} {shq(str(int(timeout_seconds)))}"
            relay = LineRelay(self._relay_line) if self._on_event is not None else None
            if relay is not None and isinstance(sandbox, FollowingSandbox):

                def on_output(chunk: bytes) -> None:
                    relay.restart()
                    relay.push(decode(chunk))

                result = await sandbox.exec_following(
                    command,
                    timeout_seconds=timeout_seconds + 30,
                    follow_path=f"{run_dir}.jsonl",
                    offset=lambda: relay.offset,
                    on_output=on_output,
                )
            else:
                result = await sandbox.exec(command, timeout_seconds=timeout_seconds + 30)
            output = await sandbox.download(f"{run_dir}.jsonl")
            if relay is not None:
                relay.finish(output)
            stdout = output.decode(errors="replace")
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

    def _relay_line(self, line: str) -> None:
        if self._on_event is None or not line.strip():
            return
        try:
            event = RUNNER_EVENT_ADAPTER.validate_json(line)
        except ValueError:
            return
        self._on_event(event)


class _EventSink:
    """Delivers one run's events to `on_event`. Never raises, and passes at most one terminal event."""

    def __init__(self, agent_id: str, on_event: OnEvent) -> None:
        self._agent_id = agent_id
        self._on_event = on_event
        self._terminated = False
        self._warned = False

    def __call__(self, event: RunnerEvent) -> None:
        if self._terminated:
            return
        self._terminated = isinstance(event, (ResultEvent, ErrorEvent))
        try:
            self._on_event(event)
        except Exception as error:
            if not self._warned:
                self._warned = True
                warn("agent event handler failed", agent_id=self._agent_id, event=event.type, error=str(error))

    def relay(self, event: RunnerEvent) -> None:
        """Relayed runner events, except the terminal one, which is sent once the downloaded file validates."""
        if not isinstance(event, (ResultEvent, ErrorEvent)):
            self(event)

    def fail(self, error: AgentError) -> None:
        self(ErrorEvent(at=now(), code=_runner_code(error.code), message=str(error)))


WorkspaceFactory = Callable[[Sandbox], WorkspaceBackend]


async def run_agent(
    agent: AgentDefinition | str,
    message: str,
    *,
    event_ids: EventIds,
    sandbox: Sandbox,
    placement: Placement | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    model: Model | str | None = None,
    workspace_factory: WorkspaceFactory | None = None,
    sandbox_runner: SandboxRunner | None = None,
    storage: object | None = None,
    on_event: OnEvent | None = None,
) -> BaseModel:
    """Run one registered agent and validate its output twice.

    `placement` can safely move an in-sandbox definition into the Workflow task
    when credential brokering is unavailable. It never grants additional tools.

    `on_event` receives the run's events as they happen, in the same shape for
    both placements, ending in exactly one `result` or `error` that matches what
    this call returns or raises. In-task runs then need a streaming model, and a
    custom `sandbox_runner` relays nothing but that terminal event. Exceptions
    from `on_event` are logged and never fail the run.
    """
    del storage  # Reserved for the follow-up spend ledger/conversation store.
    definition = get_agent(agent) if isinstance(agent, str) else agent
    selected_placement = placement or definition.placement
    if definition.placement == "in-task" and selected_placement == "in-sandbox":
        raise ValueError(f"agent {definition.id} cannot be promoted from in-task to in-sandbox at runtime")
    if not message:
        raise ValueError("agent message must not be empty")
    sink = _EventSink(definition.id, on_event) if on_event is not None else None
    with measure_sandbox_requests() as sandbox_requests:
        try:
            if selected_placement == "in-task":
                output, usage = await _run_in_task(
                    definition,
                    message,
                    sandbox,
                    timeout_seconds,
                    model,
                    workspace_factory,
                    sink,
                )
            else:
                output, usage = await _run_in_sandbox(
                    definition,
                    message,
                    event_ids.trace_id,
                    sandbox,
                    timeout_seconds,
                    sandbox_runner,
                    sink,
                )
        except asyncio.CancelledError:
            if sink is not None:
                sink.fail(AgentError(definition.id, "agent_error", "agent run was cancelled"))
            raise
        except Exception as error:
            failure = _agent_error(definition.id, error)
            if sink is not None:
                sink.fail(failure)
            if failure is error:
                raise
            raise failure from error
    emit(
        FactoryEvent(
            trace_id=event_ids.trace_id,
            type="agent.finished",
            payload={
                "agent_id": definition.id,
                "placement": selected_placement,
                "usage": usage,
                "sandbox_requests": sandbox_requests.count,
            },
            run_id=event_ids.run_id,
            repo=event_ids.repo,
            subject=event_ids.subject,
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
    sink: _EventSink | None,
) -> tuple[BaseModel, dict[str, Any]]:
    workspace = workspace_factory(sandbox) if workspace_factory else SandboxWorkspaceBackend(sandbox)
    built = definition.build(model=model)
    deps = AgentDeps(read_only="edit" not in definition.tools and "write" not in definition.tools)
    mapper = RunnerEventMapper()
    if sink is not None:
        sink(started_event(definition.id, model if isinstance(model, str) else definition.model))
    try:
        result = await built.run(
            message,
            deps=deps,
            usage_limits=UsageLimits(request_limit=definition.max_turns),
            timeout=timeout_seconds,
            workspace=workspace,
            event_stream_handler=mapper.stream_handler(sink) if sink is not None else None,
        )
    finally:
        if sink is not None:
            for event in mapper.end_response():
                sink(event)
    output = definition.output_type.model_validate(result.output.model_dump(mode="json", by_alias=True))
    usage = result.usage
    if sink is not None:
        sink(UsageEvent(at=now(), usage=usage_from(usage)))
        sink(ResultEvent(at=now(), output=output.model_dump(mode="json", by_alias=True), turns=usage.requests))
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
    sink: _EventSink | None,
) -> tuple[BaseModel, dict[str, Any]]:
    run_input = RunnerInput(
        protocol_version=4,
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
    runner = sandbox_runner or UploadedSandboxRunner(sink.relay if sink is not None else None)
    exit_code, stdout = await runner.run(sandbox, run_input, timeout_seconds)
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
    if sink is not None:
        sink(terminal)
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


def _agent_error(agent_id: str, error: Exception) -> AgentError:
    if isinstance(error, AgentError):
        return error
    if isinstance(error, UsageLimitExceeded):
        return AgentError(agent_id, "max_turns", str(error))
    if isinstance(error, RunTimedOut):
        return AgentError(agent_id, "timeout", str(error))
    if isinstance(error, UnexpectedModelBehavior):
        return AgentError(agent_id, "invalid_submission", str(error))
    if isinstance(error, ValidationError):
        return AgentError(agent_id, "output_invalid", str(error))
    return AgentError(agent_id, "agent_error", str(error))


def _runner_code(code: AgentErrorCode) -> RunnerErrorCode:
    if code == "output_invalid":
        return "invalid_submission"
    if code == "runner_lost":
        return "agent_error"
    return code
