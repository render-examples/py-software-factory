"""PR checkout validation and merge-base discovery."""

from __future__ import annotations

import re
from typing import Protocol

from factory.claims import ExpectedFailure
from factory.sandbox import ExecResult, shq

_HEAD_MOVED = "the PR changed after it was labeled; reapply the label"


class MergeBaseSandbox(Protocol):
    async def exec(
        self, command: str, *, timeout_seconds: float | None = None, as_runner: bool = False
    ) -> ExecResult: ...


def head_moved() -> ExpectedFailure:
    return ExpectedFailure(_HEAD_MOVED, "head_moved")


async def merge_base(sandbox: MergeBaseSandbox, base_branch: str) -> str:
    result = await sandbox.exec(
        f"git merge-base {shq(f'refs/remotes/origin/{base_branch}')} HEAD",
        timeout_seconds=60,
        as_runner=True,
    )
    sha = str(result.stdout).strip()
    if result.exit_code != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ExpectedFailure(
            f"couldn't find where this PR branched from `{base_branch}`, so there's no diff to review.",
            "no_merge_base",
        )
    return sha
