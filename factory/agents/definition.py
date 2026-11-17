from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import Agent, ToolOutput
from pydantic_ai.models import Model

from factory.agents.tools import AgentDeps, ToolName, create_tools

Placement = Literal["in-task", "in-sandbox"]


@dataclass(frozen=True)
class AgentDefinition:
    """Stable configuration shared by the host and the sandbox runner."""

    id: str
    prompt_path: Path
    tools: tuple[ToolName, ...]
    output_type: type[BaseModel]
    max_turns: int
    model: str
    placement: Placement

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("agent id must not be empty")
        if self.max_turns <= 0:
            raise ValueError(f"agent {self.id}: max_turns must be positive")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError(f"agent {self.id}: tools must not contain duplicates")

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
        system_prompt: str | None = None,
        tools: tuple[ToolName, ...] | None = None,
    ) -> Agent[AgentDeps, BaseModel]:
        """Build a fresh agent so tests and runs can inject a model safely."""
        selected_tools = self.tools if tools is None else tools
        if not set(selected_tools).issubset(self.tools):
            raise ValueError(f"agent {self.id}: requested tools exceed its authority")
        return Agent(
            model or self.model,
            output_type=ToolOutput(
                self.output_type,
                name="submit",
                description="Submit the final structured result.",
            ),
            system_prompt=self.prompt if system_prompt is None else system_prompt,
            deps_type=AgentDeps,
            tools=create_tools(selected_tools),
            name=self.id,
            retries=1,
        )
