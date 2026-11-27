from __future__ import annotations

from typing import Literal

AgentErrorCode = Literal["timeout", "max_turns", "invalid_submission", "output_invalid", "agent_error"]


class AgentError(Exception):
    """Every way an agent run can fail. The message can quote repository output, so it never goes to GitHub."""

    def __init__(self, agent_id: str, code: AgentErrorCode, message: str) -> None:
        super().__init__(message)
        self.agent_id = agent_id
        self.code: AgentErrorCode = code
