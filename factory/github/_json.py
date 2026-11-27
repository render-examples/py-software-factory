from __future__ import annotations

from typing import Any, cast


def get(value: object, key: str) -> Any:
    if isinstance(value, dict):
        return cast("dict[str, Any]", value).get(key)
    return None
