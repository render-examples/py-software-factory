from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic_ai import RunContext
from pydantic_ai.tools import Tool
from pydantic_ai.workspaces import CommandResult

from factory.agents.shell_policy import reject_reason
from factory.sandbox.types import APP_DIR, SCRATCH_DIR

ToolName = Literal["read", "ls", "bash", "edit", "write"]

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 1800
DEFAULT_MAX_LINES = 2_000
DEFAULT_MAX_BYTES = 50 * 1024
LS_DEFAULT_LIMIT = 500
LS_MAX_LIMIT = 2_000
AS_RUNNER = f"{APP_DIR}/as-runner.sh"


@dataclass(frozen=True)
class AgentDeps:
    """Per-run tool behavior; secrets are deliberately absent."""

    read_only: bool
    as_runner: bool = False
    scratch_dir: str = SCRATCH_DIR


def create_tools(names: tuple[ToolName, ...]) -> list[Tool[AgentDeps]]:
    functions = {"read": read, "ls": ls, "bash": bash, "edit": edit, "write": write}
    return [Tool(cast(Any, functions[name])) for name in names]


async def read(ctx: RunContext[AgentDeps], path: str, offset: int = 1, limit: int = DEFAULT_MAX_LINES) -> str:
    """Read a text file. offset is the first line (1-based), and limit bounds returned lines."""
    if offset < 1:
        raise ValueError("offset must be at least 1")
    limit = min(max(limit, 1), DEFAULT_MAX_LINES)
    if ctx.deps.as_runner:
        text = await _remote_read(ctx, path)
    else:
        text = await ctx.workspace.read_text(path)
    lines = text.splitlines()
    selected = "\n".join(lines[offset - 1 : offset - 1 + limit])
    return _truncate(selected)


async def ls(ctx: RunContext[AgentDeps], path: str = ".", limit: int = LS_DEFAULT_LIMIT) -> str:
    """List a directory in sorted order, with a trailing slash on directories."""
    limit = min(max(limit, 1), LS_MAX_LIMIT)
    if ctx.deps.as_runner:
        result = await _runner_command(ctx, ["python3", "-I", "-c", _LS_SCRIPT, path])
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or result.stdout)
        names = result.stdout.splitlines()
    else:
        entries = await ctx.workspace.list_dir(path)
        names = sorted(f"{entry.name}/" if entry.is_dir else entry.name for entry in entries)
    shown = names[:limit]
    text = "\n".join(shown) if shown else "(empty directory)"
    if len(names) > limit:
        text += f"\n\n[{len(names) - limit} more entries; raise limit or use bash]"
    return text


async def bash(
    ctx: RunContext[AgentDeps],
    command: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109
) -> str:
    """Run a bash command in the repository with a bounded timeout."""
    if ctx.deps.read_only and (reason := reject_reason(command, ctx.deps.scratch_dir)):
        raise ValueError(
            f"Command rejected by the read-only shell policy: {reason}. "
            "This agent cannot modify the working directory; inspect with read-only commands instead."
        )
    timeout = min(max(timeout, 1), MAX_TIMEOUT_SECONDS)
    if ctx.deps.as_runner:
        result = await _runner_command(ctx, ["bash", "-lc", command], timeout=float(timeout))
    else:
        result = await ctx.workspace.run(  # noqa: S604
            command,
            shell=True,
            env=_shell_env(ctx.deps.scratch_dir),
            timeout=float(timeout),
        )
    output = result.stdout
    if result.stderr:
        output += ("\n" if output else "") + result.stderr
    if result.exit_code:
        output += f"\n[exit code {result.exit_code}]"
    return _truncate(output)


async def edit(ctx: RunContext[AgentDeps], path: str, old_text: str, new_text: str) -> str:
    """Replace exactly one occurrence of old_text in a text file."""
    if ctx.deps.read_only:
        raise PermissionError("this agent has no edit authority")
    if not old_text:
        raise ValueError("old_text must not be empty")
    if ctx.deps.as_runner:
        encoded = [base64.b64encode(value.encode()).decode() for value in (old_text, new_text)]
        result = await _runner_command(ctx, ["python3", "-I", "-c", _EDIT_SCRIPT, path, *encoded])
        if result.exit_code != 0:
            raise ValueError(result.stderr.strip() or "edit failed")
    else:
        content = await ctx.workspace.read_text(path)
        count = content.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once; found {count}")
        await ctx.workspace.write_text(path, content.replace(old_text, new_text, 1))
    return f"edited {path}"


async def write(ctx: RunContext[AgentDeps], path: str, content: str) -> str:
    """Create or replace a UTF-8 text file."""
    if ctx.deps.read_only:
        raise PermissionError("this agent has no write authority")
    if ctx.deps.as_runner:
        encoded = base64.b64encode(content.encode()).decode()
        result = await _runner_command(ctx, ["python3", "-I", "-c", _WRITE_SCRIPT, path, encoded])
        if result.exit_code != 0:
            raise ValueError(result.stderr.strip() or "write failed")
    else:
        await ctx.workspace.write_text(path, content)
    return f"wrote {path}"


async def _remote_read(ctx: RunContext[AgentDeps], path: str) -> str:
    result = await _runner_command(ctx, ["python3", "-I", "-c", _READ_SCRIPT, path])
    if result.exit_code != 0:
        raise OSError(result.stderr.strip() or f"could not read {path}")
    return result.stdout


async def _runner_command(
    ctx: RunContext[AgentDeps],
    argv: list[str],
    *,
    timeout: float | None = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109
) -> CommandResult:
    return await ctx.workspace.run(["bash", AS_RUNNER, *argv], shell=False, timeout=timeout)


def _shell_env(scratch_dir: str) -> dict[str, str]:
    return {
        "HOME": scratch_dir,
        "TMPDIR": scratch_dir,
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*",
    }


def _truncate(text: str) -> str:
    lines = text.splitlines()
    if len(lines) > DEFAULT_MAX_LINES:
        text = "\n".join(lines[-DEFAULT_MAX_LINES:])
        text = "…[earlier lines truncated]\n" + text
    encoded = text.encode()
    if len(encoded) > DEFAULT_MAX_BYTES:
        text = encoded[-DEFAULT_MAX_BYTES:].decode(errors="replace")
        text = "…[earlier output truncated]\n" + text
    return text


_READ_SCRIPT = "import pathlib,sys;sys.stdout.write(pathlib.Path(sys.argv[1]).read_text())"
_LS_SCRIPT = (
    "import pathlib,sys;"
    "p=pathlib.Path(sys.argv[1]);"
    "print('\\n'.join(sorted(x.name+('/' if x.is_dir() else '') for x in p.iterdir())))"
)
_WRITE_SCRIPT = (
    "import base64,pathlib,sys;"
    "p=pathlib.Path(sys.argv[1]);p.parent.mkdir(parents=True,exist_ok=True);"
    "p.write_bytes(base64.b64decode(sys.argv[2]))"
)
_EDIT_SCRIPT = (
    "import base64,pathlib,sys;"
    "p=pathlib.Path(sys.argv[1]);s=p.read_text();o=base64.b64decode(sys.argv[2]).decode();"
    "n=base64.b64decode(sys.argv[3]).decode();c=s.count(o);"
    "c==1 or sys.exit(f'old_text must occur exactly once; found {c}');"
    "p.write_text(s.replace(o,n,1))"
)
