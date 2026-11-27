from __future__ import annotations


def shq(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"
