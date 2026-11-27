from __future__ import annotations

from starlette.requests import Request

MAX_BODY_BYTES = 256 * 1024


async def read_body(request: Request, max_bytes: int = MAX_BODY_BYTES) -> bytes | None:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)
