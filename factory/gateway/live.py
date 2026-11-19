"""Partial assistant messages, rebuilt from `live` notices, so a reader who connects mid-message sees it so far."""

from __future__ import annotations

import time
from dataclasses import dataclass

from factory.events import LiveDelta, LiveFinal

STALE_SECONDS = 15 * 60


@dataclass(frozen=True)
class PartialMessage:
    stream: str
    thread: str | None
    message_id: str
    text: str


@dataclass(frozen=True)
class _Entry:
    message: PartialMessage
    local_seq: int
    part: int
    updated: float


class LiveCache:
    def __init__(self) -> None:
        self._messages: dict[tuple[str, str], _Entry] = {}

    def apply(self, notice: LiveDelta | LiveFinal, now: float | None = None) -> LiveDelta | None:
        """Returns the delta to relay, or None for a final notice or a part already applied."""
        now = time.monotonic() if now is None else now
        key = (notice.stream, notice.message_id)
        if isinstance(notice, LiveFinal):
            self._messages.pop(key, None)
            return None
        current = self._messages.get(key)
        if current is not None and (notice.local_seq, notice.part) <= (current.local_seq, current.part):
            return None
        text = (current.message.text if current else "") + notice.text
        self._messages[key] = _Entry(
            PartialMessage(notice.stream, notice.thread, notice.message_id, text), notice.local_seq, notice.part, now
        )
        self._sweep(now)
        return notice

    def partials(self, key: str) -> list[PartialMessage]:
        """Messages in progress on a stream, or on a thread."""
        return [
            entry.message
            for entry in self._messages.values()
            if entry.message.stream == key or entry.message.thread == key
        ]

    def _sweep(self, now: float) -> None:
        for key in [key for key, entry in self._messages.items() if now - entry.updated > STALE_SECONDS]:
            del self._messages[key]
