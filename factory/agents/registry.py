from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from factory.agents.definition import AgentDefinition
from factory.schemas.implementation import Implementation
from factory.schemas.review import Review
from factory.schemas.spec import SpecDraft
from factory.schemas.triage import TriageDecision

_PROMPTS = Path(__file__).with_name("prompts")

TRIAGE = AgentDefinition(
    id="triage",
    prompt_path=_PROMPTS / "triage.md",
    tools=("read", "ls", "bash"),
    output_type=TriageDecision,
    max_turns=40,
    model="anthropic:claude-sonnet-5-5",
    placement="in-task",
)
SPEC_WRITER = AgentDefinition(
    id="spec-writer",
    prompt_path=_PROMPTS / "spec-writer.md",
    tools=("read", "ls", "bash"),
    output_type=SpecDraft,
    max_turns=60,
    model="anthropic:claude-opus-5-5",
    placement="in-task",
)
IMPLEMENTER = AgentDefinition(
    id="implementer",
    prompt_path=_PROMPTS / "implementer.md",
    tools=("read", "ls", "bash", "edit", "write"),
    output_type=Implementation,
    max_turns=150,
    model="anthropic:claude-opus-5-5",
    placement="in-sandbox",
)
PR_REVIEWER = AgentDefinition(
    id="pr-reviewer",
    prompt_path=_PROMPTS / "pr-reviewer.md",
    tools=("read", "ls", "bash"),
    output_type=Review,
    max_turns=100,
    model="anthropic:claude-sonnet-5-5",
    placement="in-sandbox",
)

AGENTS = (TRIAGE, SPEC_WRITER, IMPLEMENTER, PR_REVIEWER)
AGENT_REGISTRY = MappingProxyType({agent.id: agent for agent in AGENTS})


def get_agent(agent_id: str) -> AgentDefinition:
    try:
        return AGENT_REGISTRY[agent_id]
    except KeyError as error:
        raise ValueError(f"unknown agent {agent_id!r}") from error
