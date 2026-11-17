"""Map GitHub unified diffs to commentable lines on the right side."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Protocol

from factory.github import GitHubError, PullFile

DiffLines = dict[str, set[int]]
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class DiffGitHub(Protocol):
    async def get_pull_diff(self, number: int) -> str: ...


def parse_diff_lines(diff: str) -> DiffLines:
    files: DiffLines = {}
    current: set[int] | None = None
    old_left = new_left = next_line = 0
    for line in diff.split("\n"):
        if old_left > 0 or new_left > 0:
            kind = line[:1]
            if kind == "\\":
                continue
            if kind == "-":
                old_left -= 1
                continue
            if kind in {"+", " "} or line == "":
                if current is not None:
                    current.add(next_line)
                next_line += 1
                new_left -= 1
                if kind != "+":
                    old_left -= 1
                continue
            old_left = new_left = 0
        if line.startswith("diff --git "):
            current = None
        elif line.startswith("+++ "):
            path = _new_path(line[4:])
            current = None if path is None else files.setdefault(path, set())
        elif match := _HUNK.match(line):
            old_left = int(match.group(1) or 1)
            next_line = int(match.group(2))
            new_left = int(match.group(3) or 1)
    return files


def patch_lines(files: list[PullFile]) -> DiffLines:
    lines: DiffLines = {}
    for file in files:
        if file.patch is None or file.status == "removed":
            continue
        parsed = parse_diff_lines(f"+++ b/{file.filename}\n{file.patch}").get(file.filename)
        if parsed is not None:
            lines[file.filename] = parsed
    return lines


async def pull_diff_lines(gh: DiffGitHub, pr: int, files: list[PullFile]) -> DiffLines:
    try:
        return parse_diff_lines(await gh.get_pull_diff(pr))
    except GitHubError as error:
        if error.status in {406, 422}:
            return patch_lines(files)
        raise


def commentable(lines: DiffLines) -> Callable[[str, int], bool]:
    return lambda path, line: line in lines.get(path, set())


def _new_path(raw: str) -> str | None:
    name = raw.removesuffix("\t")
    if name == "/dev/null":
        return None
    if name.startswith('"') and name.endswith('"'):
        name = _unquote(name[1:-1])
    return name[2:] if name.startswith("b/") else name


def _unquote(value: str) -> str:
    data = bytearray()
    index = 0
    escapes = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
    while index < len(value):
        if value[index] != "\\":
            encoded = value[index].encode()
            data.extend(encoded)
            index += 1
            continue
        octal = re.match(r"[0-7]{3}", value[index + 1 :])
        if octal:
            data.append(int(octal.group(), 8))
            index += 4
            continue
        following = value[index + 1 : index + 2]
        data.append(escapes.get(following, ord(following) if following else 0))
        index += 2
    return data.decode("utf-8", errors="replace")
