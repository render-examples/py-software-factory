from __future__ import annotations

import re
from collections.abc import Callable, Sequence

COMMENT_MAX_CHARS = 65_536
BODY_MAX_CHARS = 60_000

_ZERO_WIDTH_SPACE = "\u200b"
_ZERO_WIDTH_JOINER = "\u200d"
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*$")

_MENTION = re.compile(r"(?<![A-Za-z0-9_.\-/@])@(?=[A-Za-z0-9])")


def _neutralize_markers(text: str) -> str:
    return text.replace("<!--", f"<{_ZERO_WIDTH_SPACE}!--")


def _neutralize_mentions(text: str) -> str:
    return _map_outside_fences(text, lambda prose: _MENTION.sub(f"@{_ZERO_WIDTH_JOINER}", prose))


def sanitize(text: str) -> str:
    return _neutralize_mentions(_neutralize_markers(_close_fences(text.strip())))


def sanitize_inline(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    return _MENTION.sub(f"@{_ZERO_WIDTH_JOINER}", _neutralize_markers(collapsed))


def _longest_backtick_run(text: str) -> int:
    return max((len(run) for run in re.findall(r"`+", text)), default=0)


def verbatim_code_span(text: str) -> str:
    longest = _longest_backtick_run(text)
    fence = "`" * (longest + 1)
    return f"{fence} {text} {fence}" if longest > 0 or text.startswith(" ") else f"{fence}{text}{fence}"


def code_span(text: str) -> str:
    return verbatim_code_span(sanitize_inline(text))


def fenced(text: str, lang: str = "") -> str:
    fence = "`" * (max(2, _longest_backtick_run(text)) + 1)
    return f"{fence}{lang}\n{text}\n{fence}"


def _map_outside_fences(text: str, fn: Callable[[str], str]) -> str:
    out: list[str] = []
    prose: list[str] = []
    fence: str | None = None

    def flush() -> None:
        if prose:
            out.append(fn("\n".join(prose)))
            prose.clear()

    for line in text.split("\n"):
        if fence:
            out.append(line)
            if _closes_fence(line, fence):
                fence = None
            continue
        fence = _opens_fence(line)
        if fence:
            flush()
            out.append(line)
        else:
            prose.append(line)
    flush()
    return "\n".join(out)


def _opens_fence(line: str) -> str | None:
    match = _FENCE_OPEN.match(line)
    if not match:
        return None
    fence = match.group(1)
    if fence.startswith("`") and "`" in line[match.end() :]:
        return None
    return fence


def _closes_fence(line: str, fence: str) -> bool:
    match = _FENCE_CLOSE.match(line)
    return bool(match) and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence)  # pyright: ignore[reportOptionalMemberAccess]


def _open_fence(text: str) -> str | None:
    fence: str | None = None
    for line in text.split("\n"):
        if not fence:
            fence = _opens_fence(line)
        elif _closes_fence(line, fence):
            fence = None
    return fence


def _close_fences(text: str) -> str:
    fence = _open_fence(text)
    return f"{text}\n{fence}" if fence else text


def truncate(text: str, max_chars: int, notice: str = "\n\n_(truncated)_") -> str:
    if len(text) <= max_chars:
        return text
    budget = max_chars - len(notice) - 8
    if budget <= 0:
        return text[: max(0, max_chars)]
    cut = budget
    newline = text.rfind("\n", 0, cut + 1)
    if newline > budget * 0.8:
        cut = newline
    kept = text[:cut].rstrip()
    fence = _open_fence(kept)
    if fence:
        kept = f"{kept}\n{fence}"
    result = f"{kept}{notice}"
    return result if len(result) <= max_chars else result[:max_chars]


def fit_around(head: str, middle: str, tail: str, max_chars: int) -> str:
    room = max_chars - len(head) - len(tail)
    return f"{head}{truncate(middle, max(0, room))}{tail}"


def bullets(items: Sequence[str]) -> str:
    return "\n".join(f"- {sanitize_inline(item)}" for item in items)
