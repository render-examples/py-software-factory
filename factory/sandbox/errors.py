"""Typed sandbox lifecycle failures that stages can translate into outcomes."""

from __future__ import annotations


class SandboxHeadMovedError(RuntimeError):
    def __init__(self, repository: str, pull_request: int, actual: str, expected: str) -> None:
        super().__init__(f"head moved: {repository}#{pull_request} is at {actual}, expected {expected}")
        self.repository = repository
        self.pull_request = pull_request
        self.actual = actual
        self.expected = expected
