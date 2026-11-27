from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import logfire

from factory.format import code_span, fenced
from factory.sandbox import ExecResult

_COMMAND_TIMEOUT_SECONDS = 20 * 60
_OUTPUT_TAIL_CHARS = 4096


class VerifySandbox(Protocol):
    async def exec(
        self, command: str, *, timeout_seconds: float | None = None, as_runner: bool = False
    ) -> ExecResult: ...


@dataclass(frozen=True)
class VerifyRun:
    command: str
    passed: bool
    exit_code: int | None
    output: str
    duration_ms: int


async def run_verify(sandbox: VerifySandbox, commands: tuple[str, ...]) -> list[VerifyRun]:
    runs: list[VerifyRun] = []
    for command in commands:
        with logfire.span("verify {command}", command=command) as span:
            started = time.monotonic()
            try:
                result = await sandbox.exec(command, timeout_seconds=_COMMAND_TIMEOUT_SECONDS, as_runner=True)
                output = "\n".join(part for part in (result.stdout.rstrip(), result.stderr.rstrip()) if part)
                run = VerifyRun(
                    command,
                    result.exit_code == 0,
                    result.exit_code,
                    _tail(output),
                    round((time.monotonic() - started) * 1000),
                )
            except Exception as error:
                run = VerifyRun(
                    command,
                    False,
                    None,
                    _tail(f"The command did not finish: {error}"),
                    round((time.monotonic() - started) * 1000),
                )
            span.set_attributes({"passed": run.passed, "exit_code": run.exit_code})
            if not run.passed:
                span.set_level("warn")
            runs.append(run)
    return runs


def all_passed(runs: list[VerifyRun]) -> bool:
    return all(run.passed for run in runs)


def failure_summary(runs: list[VerifyRun]) -> str:
    return ", ".join(f"{code_span(run.command)} ({_exit_text(run)})" for run in runs if not run.passed)


def failure_report(runs: list[VerifyRun]) -> str:
    sections = [
        f"### {code_span(run.command)} ({_exit_text(run)})\n\n{fenced(run.output or '(no output)', 'text')}"
        for run in runs
        if not run.passed
    ]
    return "\n\n".join(
        [
            "## Failing verify output",
            "The workflow reran the verify commands after your last attempt. Fix every failure below.",
            *sections,
        ]
    )


def _exit_text(run: VerifyRun) -> str:
    return "did not finish" if run.exit_code is None else f"exit {run.exit_code}"


def _tail(text: str) -> str:
    return text if len(text) <= _OUTPUT_TAIL_CHARS else f"…{text[-_OUTPUT_TAIL_CHARS:]}"
