from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Literal

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.tools import Tool

from factory.agents.shell_policy import reject_reason
from factory.sandbox.types import APP_DIR, RUNNER_HOME, RUNNER_PATH, RUNNER_TMP, SCRATCH_DIR

ToolName = Literal["read", "ls", "bash", "edit", "write"]

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 1800
DEFAULT_MAX_LINES = 2_000
DEFAULT_MAX_BYTES = 50 * 1024
LS_DEFAULT_LIMIT = 500
LS_MAX_LIMIT = 2_000


@dataclass(frozen=True)
class AgentDeps:
    """Per-run tool behavior; secrets are deliberately absent."""

    read_only: bool
    scratch_dir: str = SCRATCH_DIR


def create_tools(names: tuple[ToolName, ...]) -> list[Tool[AgentDeps]]:
    tools: list[Tool[AgentDeps]] = []
    for name in names:
        match name:
            case "read":
                tools.append(Tool(read))
            case "ls":
                tools.append(Tool(ls))
            case "bash":
                tools.append(Tool(bash))
            case "edit":
                tools.append(Tool(edit))
            case "write":
                tools.append(Tool(write))
    return tools


async def read(ctx: RunContext[AgentDeps], path: str, offset: int = 1, limit: int = DEFAULT_MAX_LINES) -> str:
    """Read a text file. offset is the first line (1-based), and limit bounds returned lines."""
    if offset < 1:
        raise ValueError("offset must be at least 1")
    limit = min(max(limit, 1), DEFAULT_MAX_LINES)
    text = await ctx.workspace.read_text(await _repo_path(ctx, path))
    lines = text.splitlines()
    selected = "\n".join(lines[offset - 1 : offset - 1 + limit])
    return _truncate(selected)


async def ls(ctx: RunContext[AgentDeps], path: str = ".", limit: int = LS_DEFAULT_LIMIT) -> str:
    """List a directory in sorted order, with a trailing slash on directories."""
    limit = min(max(limit, 1), LS_MAX_LIMIT)
    entries = await ctx.workspace.list_dir(await _repo_path(ctx, path))
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
        raise ModelRetry(
            f"Command rejected by the read-only shell policy: {reason}. "
            "This agent cannot modify the working directory; inspect with read-only commands instead."
        )
    timeout = min(max(timeout, 1), MAX_TIMEOUT_SECONDS)
    result = await ctx.workspace.run(  # noqa: S604
        command,
        shell=True,
        env=_shell_env(),
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
    contained = await _repo_path(ctx, path)
    content = await ctx.workspace.read_text(contained)
    count = content.count(old_text)
    if count != 1:
        raise ValueError(f"old_text must occur exactly once; found {count}")
    await ctx.workspace.write_text(contained, content.replace(old_text, new_text, 1))
    return f"edited {path}"


async def write(ctx: RunContext[AgentDeps], path: str, content: str) -> str:
    """Create or replace a UTF-8 text file."""
    if ctx.deps.read_only:
        raise PermissionError("this agent has no write authority")
    await ctx.workspace.write_text(await _repo_path(ctx, path), content)
    return f"wrote {path}"


async def _repo_path(ctx: RunContext[AgentDeps], path: str) -> str:
    root = await ctx.workspace.working_dir()
    resolved = await ctx.workspace.resolve(path)
    root_real = await ctx.workspace.realpath(root)
    target_real = await ctx.workspace.realpath(resolved)
    if not _is_within(resolved, root) or not _is_within(target_real, root_real):
        raise ModelRetry(f"path {path!r} escapes the repository workspace")
    return resolved


def _is_within(path: str, root: str) -> bool:
    try:
        return posixpath.commonpath((path, root)) == root
    except ValueError:
        return False


def _shell_env() -> dict[str, str]:
    return {
        "HOME": RUNNER_HOME,
        "TMPDIR": RUNNER_TMP,
        "PATH": RUNNER_PATH,
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "GIT_CONFIG_GLOBAL": f"{APP_DIR}/gitconfig",
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
