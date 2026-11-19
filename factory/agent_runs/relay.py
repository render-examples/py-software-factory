from __future__ import annotations

from collections.abc import Callable

_ENCODING = "utf-8"
_ERRORS = "surrogateescape"


def decode(data: bytes) -> str:
    """Decode bytes so `LineRelay` counts them back exactly, even split or invalid UTF-8."""
    return data.decode(_ENCODING, _ERRORS)


class LineRelay:
    """Relays complete lines of a growing file and counts their bytes, so a reader can resume at `offset`."""

    def __init__(self, on_line: Callable[[str], None]) -> None:
        self._on_line = on_line
        self._offset = 0
        self._partial = ""

    @property
    def offset(self) -> int:
        """Bytes of complete lines relayed so far, including their newlines."""
        return self._offset

    def push(self, data: str) -> None:
        *lines, self._partial = (self._partial + data).split("\n")
        for line in lines:
            self._offset += len(line.encode(_ENCODING, _ERRORS)) + 1
            self._on_line(line)

    def restart(self) -> None:
        """Drop the partial trailing line; the next read starts again at `offset`."""
        self._partial = ""

    def finish(self, file: bytes) -> None:
        """Relay the lines of the whole file that haven't been relayed yet."""
        self.restart()
        self.push(decode(file[self._offset :]))
