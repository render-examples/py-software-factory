"""Parsing and checking the changes file `collect-changes.sh` writes. Nothing in it is trusted until checked."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

_SHA = re.compile(rb"^[0-9a-f]{40}$")
_ZERO_SHA = "0" * 40
_RAW_HEADER = re.compile(rb"^:(\d{6}) (\d{6}) ([0-9a-f]{40}) ([0-9a-f]{40}) ([ADMT])$")
_COUNT = re.compile(rb"^\d{1,15}$")
_SIZE_LINE = re.compile(rb"^([0-9a-f]{40}) (\d{1,15})$")
_BLOB_HEADER = re.compile(rb"^([0-9a-f]{40}) blob (\d{1,15})$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MAGIC = b"software-factory-changes 1"
_WORKFLOWS_DIR = ".github/workflows/"

ChangeStatus = Literal["A", "M", "D", "T"]
RefusalKind = Literal[
    "no_changes", "too_many_files", "file_too_large", "symlink", "gitlink", "workflow_file", "unsafe_path"
]


@dataclass(frozen=True)
class ChangedFile:
    status: ChangeStatus
    path: str
    mode: str
    """The new mode; "000000" for a deletion."""
    sha: str | None
    """The new blob SHA; None for a deletion."""


@dataclass(frozen=True)
class Refusal:
    kind: RefusalKind
    detail: str
    path: str | None = None
    count: int | None = None
    bytes: int | None = None
    limit: int | None = None


@dataclass(frozen=True)
class PublishLimits:
    max_changed_files: int
    max_file_bytes: int


@dataclass(frozen=True)
class ChangesFile:
    """The sections of the changes file, split but not yet trusted."""

    tree_sha: str
    """The sandbox's `git write-tree` of the staged working tree."""
    count: int
    diff: bytes
    sizes: bytes
    blobs: bytes


@dataclass(frozen=True)
class CheckedChanges:
    tree_sha: str
    files: list[ChangedFile]
    blobs: dict[str, bytes]


@dataclass(frozen=True)
class Refused:
    refused: list[Refusal]


class ChangesFileError(ValueError):
    """The changes file doesn't have the exact layout or content it claims. Never a refusal: the sandbox lied."""


def blob_sha(content: bytes) -> str:
    """Git's object ID for a blob: sha1("blob <size>\\0<content>")."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content, usedforsecurity=False).hexdigest()


def _text(data: bytes, limit: int = 60) -> str:
    return data[:limit].decode("latin-1")


def parse_changes_file(buf: bytes) -> ChangesFile:
    """Splits the output file into its sections. Raises on anything but the exact layout."""
    pos = 0

    def line() -> bytes:
        nonlocal pos
        end = buf.find(b"\n", pos)
        if end < 0:
            raise ChangesFileError("Changes file is truncated")
        text = buf[pos:end]
        pos = end + 1
        return text

    def field(name: str) -> bytes:
        text = line()
        prefix = name.encode() + b" "
        if not text.startswith(prefix):
            raise ChangesFileError(f'Changes file: expected "{name}", got "{_text(text)}"')
        return text[len(prefix) :]

    def count(name: str) -> int:
        text = field(name)
        if not _COUNT.fullmatch(text):
            raise ChangesFileError(f'Changes file: invalid {name} "{_text(text)}"')
        return int(text)

    def section(name: str) -> bytes:
        nonlocal pos
        length = count(name)
        if pos + length + 1 > len(buf) or buf[pos + length] != 0x0A:
            raise ChangesFileError(f"Changes file: {name} is truncated")
        data = buf[pos : pos + length]
        pos += length + 1
        return data

    if line() != _MAGIC:
        raise ChangesFileError("Changes file has an unknown format")
    tree_sha = field("tree")
    if not _SHA.fullmatch(tree_sha):
        raise ChangesFileError(f'Changes file: invalid tree SHA "{_text(tree_sha)}"')
    parsed = ChangesFile(
        tree_sha=tree_sha.decode(),
        count=count("count"),
        diff=section("diff"),
        sizes=section("sizes"),
        blobs=section("blobs"),
    )
    if pos != len(buf):
        raise ChangesFileError("Changes file has trailing data")
    return parsed


@dataclass(frozen=True)
class _RawChange:
    file: ChangedFile
    raw_path: bytes


def _parse_raw_diff(diff: bytes) -> list[_RawChange]:
    """Parses `git diff-tree -r -z --no-renames --no-abbrev` output strictly, keeping each path's raw bytes."""
    files: list[_RawChange] = []
    pos = 0

    def field() -> bytes:
        nonlocal pos
        end = diff.find(b"\0", pos)
        if end < 0:
            raise ChangesFileError("Diff ends in a partial record")
        data = diff[pos:end]
        pos = end + 1
        return data

    while pos < len(diff):
        header = field()
        raw_path = field()
        match = _RAW_HEADER.fullmatch(header)
        if not match:
            raise ChangesFileError(f'Malformed diff header "{_text(header, 120)}"')
        old_mode, new_mode, old_sha, new_sha, status = (g.decode() for g in match.groups())
        if not raw_path:
            raise ChangesFileError("Diff record with an empty path")
        if status == "D":
            if new_mode != "000000" or new_sha != _ZERO_SHA:
                raise ChangesFileError(f'Malformed deletion "{_text(header, 120)}"')
        else:
            if new_mode not in ("100644", "100755", "120000", "160000") or new_sha == _ZERO_SHA:
                raise ChangesFileError(f'Malformed diff record "{_text(header, 120)}"')
            if status == "A" and (old_mode != "000000" or old_sha != _ZERO_SHA):
                raise ChangesFileError(f'Malformed addition "{_text(header, 120)}"')
        path = raw_path.decode("utf-8", errors="replace")
        change_status: ChangeStatus = status  # pyright: ignore[reportAssignmentType]
        sha = None if status == "D" else new_sha
        files.append(_RawChange(ChangedFile(change_status, path, new_mode, sha), raw_path))
    return files


def _parse_sizes(sizes: bytes) -> dict[str, int]:
    """Parses "<sha> <size>\\n" lines."""
    result: dict[str, int] = {}
    if not sizes:
        return result
    if not sizes.endswith(b"\n"):
        raise ChangesFileError("Sizes section is truncated")
    for size_line in sizes[:-1].split(b"\n"):
        match = _SIZE_LINE.fullmatch(size_line)
        if not match or match.group(1).decode() in result:
            raise ChangesFileError(f'Malformed size line "{_text(size_line, 80)}"')
        result[match.group(1).decode()] = int(match.group(2))
    return result


def _parse_blobs(blobs: bytes, expected: dict[str, int]) -> dict[str, bytes]:
    """Parses `git cat-file --batch` output and checks every object's bytes against its SHA."""
    result: dict[str, bytes] = {}
    pos = 0
    while pos < len(blobs):
        end = blobs.find(b"\n", pos)
        if end < 0:
            raise ChangesFileError("Blob section is truncated")
        header = blobs[pos:end]
        match = _BLOB_HEADER.fullmatch(header)
        if not match:
            raise ChangesFileError(f'Malformed blob header "{_text(header, 80)}"')
        sha = match.group(1).decode()
        size = int(match.group(2))
        if expected.get(sha) != size:
            raise ChangesFileError(f"Unexpected blob {sha} ({size} bytes)")
        if sha in result:
            raise ChangesFileError(f"Blob {sha} appears twice")
        start = end + 1
        if start + size + 1 > len(blobs) or blobs[start + size] != 0x0A:
            raise ChangesFileError(f"Blob {sha} is truncated")
        content = blobs[start : start + size]
        if blob_sha(content) != sha:
            raise ChangesFileError(f"Blob hash mismatch for {sha}")
        result[sha] = content
        pos = start + size + 1
    for sha in expected:
        if sha not in result:
            raise ChangesFileError(f"Blob {sha} is missing")
    return result


def _unsafe_path(path: str, raw_path: bytes) -> str | None:
    """Why a path can't be published, or None. Paths come from git, but the sandbox could forge them."""
    if path.encode() != raw_path:
        return "is not valid UTF-8"
    if _CONTROL.search(path):
        return "contains a control character"
    if path.startswith("/"):
        return "is absolute"
    for segment in path.split("/"):
        if segment in ("", ".", ".."):
            return "has an empty, '.', or '..' segment"
        if segment.lower() == ".git":
            return "is inside .git"
    return None


def _js_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def check_changes(file: ChangesFile, limits: PublishLimits) -> CheckedChanges | Refused:
    """Applies every publishing rule to the parsed file.

    Rules are re-derived here from the diff and sizes rather than trusted from the sandbox; blob bytes are checked
    only when nothing is refused.
    """
    if file.count > limits.max_changed_files:
        detail = f"{file.count} files changed; the limit is {limits.max_changed_files}"
        return Refused([Refusal("too_many_files", detail, count=file.count, limit=limits.max_changed_files)])
    changes = _parse_raw_diff(file.diff)
    if len(changes) != file.count:
        raise ChangesFileError(f"Diff has {len(changes)} records but the sandbox counted {file.count}")
    if not changes:
        return Refused([Refusal("no_changes", "Nothing changed against the base commit")])

    sizes = _parse_sizes(file.sizes)
    needed: dict[str, int] = {}
    refused: list[Refusal] = []
    seen: set[str] = set()
    for change in changes:
        f = change.file
        path = f.path
        if path in seen:
            raise ChangesFileError(f"Diff lists {path} twice")
        seen.add(path)
        unsafe = _unsafe_path(path, change.raw_path)
        if unsafe:
            refused.append(Refusal("unsafe_path", f"Path {_js_string(path)} {unsafe}", path=path))
            continue
        if path.startswith(_WORKFLOWS_DIR):
            refused.append(Refusal("workflow_file", f"Changes to {path} are not published", path=path))
        if f.sha is None:
            continue
        if f.mode == "120000":
            refused.append(Refusal("symlink", f"{path} is a symlink", path=path))
        if f.mode == "160000":
            refused.append(Refusal("gitlink", f"{path} is a submodule", path=path))
        if f.mode not in ("100644", "100755"):
            continue
        size = sizes.get(f.sha)
        if size is None:
            raise ChangesFileError(f"Sandbox reported no size for {path}")
        if size > limits.max_file_bytes:
            detail = f"{path} is {size} bytes; the limit is {limits.max_file_bytes}"
            refused.append(Refusal("file_too_large", detail, path=path, bytes=size, limit=limits.max_file_bytes))
        needed[f.sha] = size
    if refused:
        return Refused(refused)
    if len(sizes) != len(needed):
        raise ChangesFileError("Sandbox reported sizes for blobs the diff doesn't use")
    if needed and not file.blobs:
        raise ChangesFileError("Sandbox withheld blob contents without a refusal")

    return CheckedChanges(
        tree_sha=file.tree_sha,
        files=[c.file for c in changes],
        blobs=_parse_blobs(file.blobs, needed),
    )
