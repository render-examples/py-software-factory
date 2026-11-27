"""The host side of `sandbox/fileops.py`: one request per file operation, run where the files are."""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import uuid4

from factory.sandbox import APP_DIR, RUN_AS, RUNNER_TMP, SANDBOX_PYTHON, Sandbox, shq

FILEOPS = f"{APP_DIR}/fileops.py"
INLINE_COMMAND_BYTES = 96 * 1024
"""Linux caps one argument at 128 KiB, and the whole command travels as one. Larger requests are uploaded."""
TIMEOUT_SECONDS = 30
"""Under the streaming-exec ceiling, so each operation is one request. The helper bounds its searches below it."""


class FileOpRefused(Exception):
    """The helper refused a request the model can correct; the message is for the model."""


class SandboxFiles:
    def __init__(self, sandbox: Sandbox, root: str) -> None:
        self._sandbox = sandbox
        self._root = root

    async def request(self, op: str, **fields: object) -> dict[str, Any]:
        payload = json.dumps({"op": op, "root": self._root, **fields}, separators=(",", ":"))
        inline = f"{SANDBOX_PYTHON} -I {shq(FILEOPS)} {shq(payload)}"
        # RenderSandbox quotes the command again to run it as the runner user.
        if len(shq(inline).encode()) <= INLINE_COMMAND_BYTES:
            result = await self._sandbox.exec(inline, timeout_seconds=TIMEOUT_SECONDS, as_runner=True)
        else:
            staged = f"{RUNNER_TMP}/fileops-{uuid4().hex}.json"
            await self._sandbox.upload(staged, payload.encode())
            result = await self._sandbox.exec(
                f"chown {shq(RUN_AS)}: {shq(staged)} && "
                f"bash {shq(f'{APP_DIR}/as-runner.sh')} {SANDBOX_PYTHON} -I {shq(FILEOPS)} "
                f"--request-file {shq(staged)}",
                timeout_seconds=TIMEOUT_SECONDS,
            )
        try:
            response: object = json.loads(result.stdout)
        except ValueError:
            response = None
        if result.exit_code != 0 or not isinstance(response, dict):
            detail = (result.stdout or result.stderr).strip()[-500:]
            raise RuntimeError(f"fileops {op} exited {result.exit_code}: {detail}")
        body = cast(dict[str, Any], response)
        if "error" in body:
            raise FileOpRefused(str(body["error"]))
        return body
