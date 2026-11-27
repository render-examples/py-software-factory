from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import Agent, ToolOutput
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import Model

from factory.agents.tools import TOOL_RETRIES, AgentDeps, capabilities, file_tools


@dataclass(frozen=True)
class AgentDefinition:
    id: str
    prompt_path: Path
    output_type: type[BaseModel]
    max_turns: int
    model: str
    writes: bool = False

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("agent id must not be empty")
        if self.max_turns <= 0:
            raise ValueError(f"agent {self.id}: max_turns must be positive")

    @cached_property
    def prompt(self) -> str:
        text = self.prompt_path.read_text()
        if not text.strip():
            raise ValueError(f"agent {self.id}: prompt file {self.prompt_path} is empty")
        return text

    def build(
        self,
        *,
        model: Model | str | None = None,
        extra: Sequence[AbstractCapability[AgentDeps]] = (),
    ) -> Agent[AgentDeps, BaseModel]:
        return Agent(
            model or self.model,
            output_type=ToolOutput(
                self.output_type,
                name="submit",
                description="Submit the final structured result.",
            ),
            system_prompt=self.prompt,
            deps_type=AgentDeps,
            toolsets=[file_tools(writes=self.writes)],
            capabilities=[*capabilities(writes=self.writes), *extra],
            name=self.id,
            retries=TOOL_RETRIES,
        )
