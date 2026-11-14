"""Making model-written text safe to place in a factory comment."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

COMMENT_MAX_CHARS = 65_536
"""GitHub rejects an issue comment, PR body, or review body longer than this."""
BODY_MAX_CHARS = 60_000
"""Default budget for a rendered body, leaving room for the hidden markers the caller appends."""

_ZERO_WIDTH_SPACE = "\u200b"
_ZERO_WIDTH_JOINER = "\u200d"
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*$")

_MENTION = re.compile(r"(?<![A-Za-z0-9_.\-/@])@(?=[A-Za-z0-9])")
"""An @ GitHub would read as a mention: not inside a word, an email address, a path, or a URL, and followed by a
handle."""


def _neutralize_markers(text: str) -> str:
    """Breaks every `<!--` so model text can't open an HTML comment that hides content or looks like a factory
    marker. Applies inside code too: marker lookups scan the raw body."""
    return text.replace("<!--", f"<{_ZERO_WIDTH_SPACE}!--")


def _neutralize_mentions(text: str) -> str:
    """Inserts a zero-width joiner after each @ that would mention a user or team, so model text never pings
    anyone. Fenced code blocks are left alone (GitHub doesn't mention from them, and code is copied from them);
    inline code is neutralized too, since a stray backtick elsewhere can change where a span ends."""
    return _map_outside_fences(text, lambda prose: _MENTION.sub(f"@{_ZERO_WIDTH_JOINER}", prose))


def sanitize(text: str) -> str:
    """Model-written Markdown, made safe to place in a factory comment as a block: fences closed, markers and
    mentions neutralized."""
    return _neutralize_mentions(_neutralize_markers(_close_fences(text.strip())))


def sanitize_inline(text: str) -> str:
    """Model-written text for one line (a list item, a title, a table cell): whitespace collapsed, markers and
    mentions neutralized."""
    collapsed = re.sub(r"\s+", " ", text).strip()
    return _MENTION.sub(f"@{_ZERO_WIDTH_JOINER}", _neutralize_markers(collapsed))


def _longest_backtick_run(text: str) -> int:
    return max((len(run) for run in re.findall(r"`+", text)), default=0)


def verbatim_code_span(text: str) -> str:
    """`text` verbatim as an inline code span, with a delimiter longer than any backtick run inside it. For model
    input."""
    longest = _longest_backtick_run(text)
    fence = "`" * (longest + 1)
    return f"{fence} {text} {fence}" if longest > 0 or text.startswith(" ") else f"{fence}{text}{fence}"


def code_span(text: str) -> str:
    """`text` as an inline code span for GitHub, with markers and mentions neutralized."""
    return verbatim_code_span(sanitize_inline(text))


def fenced(text: str, lang: str = "") -> str:
    """`text` verbatim in a fenced block whose fence is longer than any backtick run inside it, so it can't close
    early."""
    fence = "`" * (max(2, _longest_backtick_run(text)) + 1)
    return f"{fence}{lang}\n{text}\n{fence}"


def _map_outside_fences(text: str, fn: Callable[[str], str]) -> str:
    """Splits `text` into fenced code blocks and the prose between them, and maps only the prose."""
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
    """The fence `line` opens, if any. A backtick fence's info string can't contain a backtick."""
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
    """The fence a block of Markdown leaves open at its end, if any."""
    fence: str | None = None
    for line in text.split("\n"):
        if not fence:
            fence = _opens_fence(line)
        elif _closes_fence(line, fence):
            fence = None
    return fence


def _close_fences(text: str) -> str:
    """Closes a fenced code block left open at the end, so it can't swallow what follows."""
    fence = _open_fence(text)
    return f"{text}\n{fence}" if fence else text


def truncate(text: str, max_chars: int, notice: str = "\n\n_(truncated)_") -> str:
    """Cuts `text` to at most `max_chars` characters, at a line break when one is near, closing any fence the cut
    leaves open and appending `notice`."""
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
    """Joins a fixed head and tail around a variable middle, truncating only the middle so the whole fits."""
    room = max_chars - len(head) - len(tail)
    return f"{head}{truncate(middle, max(0, room))}{tail}"


def bullets(items: Sequence[str]) -> str:
    """A Markdown bullet list of inline-sanitized items."""
    return "\n".join(f"- {sanitize_inline(item)}" for item in items)
