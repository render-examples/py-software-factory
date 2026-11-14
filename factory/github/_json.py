from __future__ import annotations

from typing import Any, cast


def get(value: object, key: str) -> Any:
    """`value[key]` when `value` is a JSON object, else None."""
    if isinstance(value, dict):
        return cast("dict[str, Any]", value).get(key)
    return None
