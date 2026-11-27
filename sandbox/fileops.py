"""File operations for the agents' tools, run inside the sandbox as the unprivileged user.

The host sends one JSON request and reads one JSON response, so the only file content that crosses the boundary
is the window a read returns and the text an edit or write supplies. Paths are resolved here, symlinks included,
and must stay inside the request's root. Standard library only: the sandbox runs this with its own Python.

Usage: fileops.py <request-json>
       fileops.py --request-file <path>   (the file is deleted once read)
Prints {"error": "<message for the model>"} for a request the model can correct, any other object on success,
and exits 2 with {"fault": "..."} for a malformed request.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from typing import Any

MAX_FILE_BYTES = 16 * 1024 * 1024
BINARY_PROBE_BYTES = 8192
DEFAULT_LINES = 2000
MAX_OUTPUT_CHARS = 40_000
MAX_RESULTS = 500
MAX_CONTEXT_LINES = 10
SEARCH_TIMEOUT_SECONDS = 20


class Refused(Exception):
    """A request the model can correct."""


class BadRequest(Exception):
    """A request the host should never send."""


def _text(request: dict[str, Any], key: str, default: str | None = None) -> str:
    value = request.get(key, default)
    if not isinstance(value, str):
        raise BadRequest(f"{key} must be a string")
    return value


def _int(request: dict[str, Any], key: str, default: int) -> int:
    value = request.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise BadRequest(f"{key} must be an integer")
    return value


def _flag(request: dict[str, Any], key: str) -> bool:
    value = request.get(key, False)
    if not isinstance(value, bool):
        raise BadRequest(f"{key} must be a boolean")
    return value


def resolve(root: str, path: str, *, for_write: bool = False) -> tuple[str, str]:
    """The real path of `path` and its form relative to the real root, refusing anything outside the root."""
    if not path or "\0" in path:
        raise Refused("path must be a non-empty string without NUL characters")
    real_root = os.path.realpath(root)
    target = os.path.realpath(path if os.path.isabs(path) else os.path.join(real_root, path))
    if target != real_root and not target.startswith(real_root + os.sep):
        raise Refused(f"{path} is outside the repository")
    relative = os.path.relpath(target, real_root)
    if for_write and relative.split(os.sep)[0] == ".git":
        raise Refused("files under .git can't be changed")
    return target, relative


def _regular_file(target: str, relative: str) -> os.stat_result:
    try:
        info = os.stat(target)
    except FileNotFoundError:
        raise Refused(f"{relative} does not exist") from None
    if stat.S_ISDIR(info.st_mode):
        raise Refused(f"{relative} is a directory; use list_files")
    if not stat.S_ISREG(info.st_mode):
        raise Refused(f"{relative} is not a regular file")
    if info.st_size > MAX_FILE_BYTES:
        raise Refused(f"{relative} is {info.st_size} bytes, more than the {MAX_FILE_BYTES} this tool reads")
    return info


def _replace(target: str, data: bytes) -> None:
    """Write `data` to `target` atomically, keeping an existing file's mode."""
    directory = os.path.dirname(target)
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".fileops-")
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def read(root: str, request: dict[str, Any]) -> dict[str, Any]:
    target, relative = resolve(root, _text(request, "path"))
    offset = _int(request, "offset", 1)
    limit = _int(request, "limit", DEFAULT_LINES)
    if offset < 1 or limit < 1:
        raise Refused("offset and limit must be at least 1")
    info = _regular_file(target, relative)
    with open(target, "rb") as source:
        data = source.read()
    if b"\0" in data[:BINARY_PROBE_BYTES]:
        return {"path": relative, "binary": True, "size": info.st_size}
    lines = data.decode("utf-8", errors="replace").splitlines()
    shown: list[str] = []
    used = 0
    end = offset - 1
    for number, line in enumerate(lines[offset - 1 : offset - 1 + limit], offset):
        entry = f"{number:6}\t{line}"
        if shown and used + len(entry) + 1 > MAX_OUTPUT_CHARS:
            break
        shown.append(entry[:MAX_OUTPUT_CHARS])
        used += len(entry) + 1
        end = number
    return {"path": relative, "text": "\n".join(shown), "start": offset, "end": end, "total": len(lines)}


def edit(root: str, request: dict[str, Any]) -> dict[str, Any]:
    target, relative = resolve(root, _text(request, "path"), for_write=True)
    edits = request.get("edits")
    if not isinstance(edits, list) or not edits:
        raise BadRequest("edits must be a non-empty list")
    _regular_file(target, relative)
    with open(target, "rb") as source:
        try:
            text = source.read().decode("utf-8")
        except UnicodeDecodeError:
            raise Refused(f"{relative} is not UTF-8 text; rewrite it with write_file") from None
    for number, change in enumerate(edits, 1):
        if not isinstance(change, dict):
            raise BadRequest("each edit must be an object")
        old, new = _text(change, "old_text"), _text(change, "new_text")
        if not old:
            raise Refused(f"edit {number}: old_text is empty")
        count = text.count(old)
        if count != 1:
            found = "was not found" if count == 0 else f"occurs {count} times; include more surrounding text"
            raise Refused(f"edit {number}: old_text {found} in {relative}. No edits were applied.")
        text = text.replace(old, new, 1)
    _replace(target, text.encode("utf-8"))
    return {"path": relative, "edits": len(edits)}


def write(root: str, request: dict[str, Any]) -> dict[str, Any]:
    target, relative = resolve(root, _text(request, "path"), for_write=True)
    data = _text(request, "content").encode("utf-8")
    existed = os.path.lexists(target)
    if existed and os.path.isdir(target):
        raise Refused(f"{relative} is a directory")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    _replace(target, data)
    return {"path": relative, "bytes": len(data), "created": not existed}


def _name_filter(glob: str) -> list[str]:
    """Match file names against `glob` as an ad hoc file type: unlike --glob, types still honor .gitignore."""
    return ["--type-add", f"fileops:{glob}", "--type", "fileops"] if glob else []


def _ripgrep(root: str, arguments: list[str]) -> tuple[list[str], bool]:
    """Run rg, returning its lines with paths relative to the root, and whether they were cut at the cap."""
    try:
        result = subprocess.run(
            ["rg", "--hidden", "--glob", "!.git", "--color", "never", *arguments],
            capture_output=True,
            text=True,
            timeout=SEARCH_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise Refused(f"the search took longer than {SEARCH_TIMEOUT_SECONDS} seconds; narrow the path") from None
    if result.returncode == 2:
        raise Refused(result.stderr.strip()[-500:] or "rg failed")
    prefix = os.path.realpath(root) + os.sep
    lines: list[str] = []
    used = 0
    for line in result.stdout.splitlines():
        line = line.removeprefix(prefix)
        if len(lines) >= MAX_RESULTS or used + len(line) + 1 > MAX_OUTPUT_CHARS:
            return lines, True
        lines.append(line)
        used += len(line) + 1
    return lines, False


def list_files(root: str, request: dict[str, Any]) -> dict[str, Any]:
    target, relative = resolve(root, _text(request, "path", "."))
    if not os.path.isdir(target):
        raise Refused(f"{relative} is not a directory")
    glob = _text(request, "glob", "")
    lines, truncated = _ripgrep(root, ["--files", "--sort", "path", *_name_filter(glob), target])
    return {"path": relative, "files": lines, "truncated": truncated}


def grep(root: str, request: dict[str, Any]) -> dict[str, Any]:
    target, relative = resolve(root, _text(request, "path", "."))
    pattern = _text(request, "pattern")
    if not pattern:
        raise Refused("pattern is empty")
    context = _int(request, "context", 0)
    if not 0 <= context <= MAX_CONTEXT_LINES:
        raise Refused(f"context must be between 0 and {MAX_CONTEXT_LINES}")
    glob = _text(request, "glob", "")
    arguments = ["--line-number", "--no-heading", "--max-columns", "300", "--max-columns-preview", "--sort", "path"]
    if _flag(request, "ignore_case"):
        arguments.append("--ignore-case")
    if _flag(request, "literal"):
        arguments.append("--fixed-strings")
    if context:
        arguments += ["--context", str(context)]
    arguments += _name_filter(glob)
    lines, truncated = _ripgrep(root, [*arguments, "--regexp", pattern, "--", target])
    return {"path": relative, "matches": lines, "truncated": truncated}


OPERATIONS: dict[str, Callable[[str, dict[str, Any]], dict[str, Any]]] = {
    "read": read,
    "edit": edit,
    "write": write,
    "list": list_files,
    "grep": grep,
}


def _respond(value: dict[str, Any]) -> None:
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["--request-file"]:
            with open(argv[1], encoding="utf-8") as staged:
                raw = staged.read()
            os.unlink(argv[1])
        else:
            [raw] = argv
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise BadRequest("the request must be an object")
        operation = OPERATIONS[_text(request, "op")]
        root = _text(request, "root")
        response = operation(root, request)
    except Refused as error:
        response = {"error": str(error)}
    except OSError as error:
        response = {"error": error.strerror or str(error)}
    except (BadRequest, KeyError, ValueError, IndexError) as error:
        _respond({"fault": f"malformed request: {error}"})
        return 2
    _respond(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
