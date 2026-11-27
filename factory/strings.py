from __future__ import annotations

import json


def json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
