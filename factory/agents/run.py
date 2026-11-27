from __future__ import annotations

from pydantic import BaseModel, ValidationError
from pydantic_ai import UsageLimits
from pydantic_ai.exceptions import RunTimedOut, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai_harness import SpendLimits
from pydantic_ai_harness.spend import SpendLimitExceeded

from factory.agents.definition import AgentDefinition
from factory.agents.errors import AgentError
from factory.agents.files import SandboxFiles
from factory.agents.registry import get_agent
from factory.agents.tools import AgentDeps
from factory.agents.workspace import SandboxWorkspace
from factory.events import EventIds, FactoryEvent, emit
from factory.sandbox import REPO_DIR, Sandbox

DEFAULT_TIMEOUT_SECONDS = 30 * 60


async def run_agent(
    agent: AgentDefinition | str,
    message: str,
    *,
    event_ids: EventIds,
    sandbox: Sandbox,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    model: Model | str | None = None,
    spend: SpendLimits[AgentDeps] | None = None,
) -> BaseModel:
    """Run one registered agent against a sandbox and validate its output a second time on the host."""
    definition = get_agent(agent) if isinstance(agent, str) else agent
    if not message:
        raise ValueError("agent message must not be empty")
    try:
        result = await definition.build(model=model, extra=(spend,) if spend else ()).run(
            message,
            deps=AgentDeps(SandboxFiles(sandbox, REPO_DIR), repo=event_ids.repo),
            usage_limits=UsageLimits(request_limit=definition.max_turns),
            timeout=timeout_seconds,
            workspace=SandboxWorkspace(sandbox),
        )
        output = definition.output_type.model_validate(result.output.model_dump(mode="json", by_alias=True))
    except SpendLimitExceeded as error:
        raise AgentError(definition.id, "spend_limit", str(error)) from error
    except UsageLimitExceeded as error:
        raise AgentError(definition.id, "max_turns", str(error)) from error
    except RunTimedOut as error:
        raise AgentError(definition.id, "timeout", str(error)) from error
    except UnexpectedModelBehavior as error:
        raise AgentError(definition.id, "invalid_submission", str(error)) from error
    except ValidationError as error:
        raise AgentError(definition.id, "output_invalid", str(error)) from error
    usage = result.usage
    emit(
        FactoryEvent(
            trace_id=event_ids.trace_id,
            type="agent.finished",
            payload={
                "agent_id": definition.id,
                "requests": usage.requests,
                "tool_calls": usage.tool_calls,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cost_usd": float(usage.cost) if usage.cost is not None else None,
            },
            run_id=event_ids.run_id,
            repo=event_ids.repo,
            subject=event_ids.subject,
        )
    )
    return output
