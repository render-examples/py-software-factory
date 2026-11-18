from __future__ import annotations


def shq(value: str) -> str:
    """Quotes one argument for a POSIX shell. Every host-built sandbox command quotes what it interpolates."""
    return "'" + value.replace("'", "'\\''") + "'"
