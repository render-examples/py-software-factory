from __future__ import annotations

import re

SAFE_REF = re.compile(r"^\w[\w./-]*$", re.ASCII)
"""Branch names the factory will clone or fetch: no leading dash, no shell or refspec metacharacters."""


def shq(value: str) -> str:
    """Quotes one argument for a POSIX shell. Every host-built sandbox command quotes what it interpolates."""
    return "'" + value.replace("'", "'\\''") + "'"
