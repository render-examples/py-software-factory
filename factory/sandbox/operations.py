from __future__ import annotations

import os
from collections.abc import Mapping

from factory.sandbox.types import ExecResult, Sandbox


def configured_group_id(environment: Mapping[str, str] | None = None) -> str:
    env = os.environ if environment is None else environment
    value = env.get("FACTORY_SANDBOX_GROUP_ID", "").strip()
    if not value.startswith("sbg-"):
        raise RuntimeError("FACTORY_SANDBOX_GROUP_ID must be an sbg-… sandbox group id")
    return value


def require_success(result: ExecResult, operation: str) -> None:
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise RuntimeError(f"{operation} exited {result.exit_code}: {detail}")


async def checked(
    sandbox: Sandbox,
    command: str,
    *,
    timeout_seconds: float,
    as_runner: bool = False,
    operation: str | None = None,
) -> str:
    result = await sandbox.exec(command, timeout_seconds=timeout_seconds, as_runner=as_runner)
    require_success(result, operation or command)
    return result.stdout
