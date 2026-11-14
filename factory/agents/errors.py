from __future__ import annotations

from typing import Literal

RunnerErrorCode = Literal["timeout", "no_submission", "invalid_input", "invalid_submission", "max_turns", "agent_error"]
"""Error codes in the runner's terminal `error` event (wire protocol)."""

AgentErrorCode = RunnerErrorCode | Literal["output_invalid", "runner_lost"]


class AgentError(Exception):
    """Every way an agent run can fail. The message can quote repository output, so it never goes to GitHub."""

    def __init__(self, agent_id: str, code: AgentErrorCode, message: str) -> None:
        super().__init__(message)
        self.agent_id = agent_id
        self.code: AgentErrorCode = code
