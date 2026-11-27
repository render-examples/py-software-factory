"""What an agent can do in its sandbox: file tools that run in the sandbox, plus Pydantic AI Harness capabilities."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai_harness import ClearToolResults, DetectLoops, Shell
from pydantic_ai_harness.repair_tool_arguments import RepairToolArguments

from factory.agents.files import FileOpRefused, SandboxFiles

READ_ONLY_COMMANDS = (
    "git",
    "rg",
    "grep",
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "find",
    "sort",
    "uniq",
    "cut",
    "diff",
    "file",
    "stat",
    "tree",
    "jq",
)
"""What read-only agents may start. Best effort, like any command filter: the sandbox is the boundary, and nothing a
read-only agent changes there is published."""
SHELL_TIMEOUT_SECONDS = 30
SHELL_OUTPUT_CHARS = 30_000
TOOL_RETRIES = 3


@dataclass(frozen=True)
class AgentDeps:
    files: SandboxFiles
    repo: str
    """"owner/name": the repository budgets are counted per."""


class TextEdit(BaseModel):
    old_text: str
    """Text that occurs exactly once in the file."""
    new_text: str


async def read_file(ctx: RunContext[AgentDeps], path: str, offset: int = 1, limit: int = 2000) -> str:
    """Read a text file with line numbers. `offset` is the first line (1-based); `limit` caps the lines shown."""
    try:
        result = await ctx.deps.files.request("read", path=path, offset=offset, limit=limit)
    except FileOpRefused as error:
        return f"Error: {error}"
    if result.get("binary"):
        return f"{result['path']} is a binary file ({result['size']} bytes)."
    total, end = int(result["total"]), int(result["end"])
    if total == 0:
        return f"{result['path']} is empty."
    text = str(result["text"])
    if end < total:
        text += f"\n[lines {result['start']}-{end} of {total}; continue with offset={end + 1}]"
    return text


async def list_files(ctx: RunContext[AgentDeps], path: str = ".", glob: str = "") -> str:
    """List files under a directory, recursively, honoring .gitignore. `glob` filters names, such as `*.py`."""
    try:
        result = await ctx.deps.files.request("list", path=path, glob=glob)
    except FileOpRefused as error:
        return f"Error: {error}"
    files = [str(name) for name in result["files"]]
    return _listing(files, bool(result["truncated"]), "No files.")


async def grep(
    ctx: RunContext[AgentDeps],
    pattern: str,
    path: str = ".",
    glob: str = "",
    ignore_case: bool = False,
    literal: bool = False,
    context: int = 0,
) -> str:
    """Search file contents with ripgrep, reporting `path:line:text`. `pattern` is a regex unless `literal`."""
    try:
        result = await ctx.deps.files.request(
            "grep", pattern=pattern, path=path, glob=glob, ignore_case=ignore_case, literal=literal, context=context
        )
    except FileOpRefused as error:
        return f"Error: {error}"
    matches = [str(line) for line in result["matches"]]
    return _listing(matches, bool(result["truncated"]), "No matches.")


async def edit_file(ctx: RunContext[AgentDeps], path: str, edits: list[TextEdit]) -> str:
    """Replace text in a file. Each `old_text` must occur exactly once when its edit applies; edits apply in order,
    and none apply unless all do."""
    try:
        result = await ctx.deps.files.request("edit", path=path, edits=[edit.model_dump() for edit in edits])
    except FileOpRefused as error:
        return f"Error: {error}"
    return f"Edited {result['path']} ({result['edits']} replacement{'s' if result['edits'] != 1 else ''})."


async def write_file(ctx: RunContext[AgentDeps], path: str, content: str) -> str:
    """Create or overwrite a UTF-8 text file, creating missing directories."""
    try:
        result = await ctx.deps.files.request("write", path=path, content=content)
    except FileOpRefused as error:
        return f"Error: {error}"
    return f"{'Created' if result['created'] else 'Wrote'} {result['path']} ({result['bytes']} bytes)."


def _listing(lines: list[str], truncated: bool, empty: str) -> str:
    if not lines:
        return empty
    text = "\n".join(lines)
    return f"{text}\n[results truncated; narrow the path or pattern]" if truncated else text


def file_tools(*, writes: bool) -> FunctionToolset[AgentDeps]:
    tools = [read_file, list_files, grep, *([edit_file, write_file] if writes else [])]
    return FunctionToolset(tools, max_retries=TOOL_RETRIES, id="files")


def capabilities(*, writes: bool) -> list[AbstractCapability[AgentDeps]]:
    if writes:
        shell = Shell[AgentDeps](
            denied_commands=[],
            default_timeout=SHELL_TIMEOUT_SECONDS,
            max_output_chars=SHELL_OUTPUT_CHARS,
            tools=["run_command"],
        )
    else:
        shell = Shell[AgentDeps](
            allowed_commands=READ_ONLY_COMMANDS,
            denied_operators=[">", ">>"],
            default_timeout=SHELL_TIMEOUT_SECONDS,
            max_output_chars=SHELL_OUTPUT_CHARS,
            tools=["run_command"],
        )
    return [
        shell,
        ClearToolResults[AgentDeps](max_tokens=120_000, keep_pairs=6, min_clear_tokens=20_000),
        DetectLoops[AgentDeps](),
        RepairToolArguments[AgentDeps](),
    ]
