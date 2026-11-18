"""Pydantic AI workspace adapter over the factory's paced sandbox client."""

from __future__ import annotations

import base64
import json
import posixpath
from collections.abc import Mapping, Sequence
from typing import cast

from pydantic_ai.workspaces import CommandResult, FileEntry, WorkspaceCommand, WorkspaceRef

from factory.sandbox import REPO_DIR, Sandbox, shq


class SandboxWorkspaceBackend:
    """Attach Pydantic AI tools to an already-opened factory sandbox.

    The factory keeps sandbox creation, pacing, retries, credentials, and cleanup
    in its own lifecycle. This backend only translates workspace operations.
    """

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox
        self.ref = WorkspaceRef(provider="render", id=sandbox.id)

    async def working_dir(self) -> str:
        return REPO_DIR

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - Pydantic AI workspace contract
    ) -> CommandResult:
        text = command if isinstance(command, str) else " ".join(shq(part) for part in command)
        if not shell and isinstance(command, str):
            text = shq(command)
        if env:
            assignments = " ".join(f"{key}={shq(value)}" for key, value in sorted(env.items()))
            text = f"env {assignments} {text}"
        result = await self.sandbox.exec(text, timeout_seconds=timeout, as_runner=True)
        return CommandResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)

    async def read_bytes(self, path: str) -> bytes:
        result = await self.sandbox.exec(
            f"python3 -I -c {shq(_READ_SCRIPT)} {shq(_remote_path(path))}",
            timeout_seconds=30,
            as_runner=True,
        )
        _check(result.exit_code, result.stderr)
        return base64.b64decode(result.stdout)

    async def write_bytes(self, path: str, data: bytes) -> None:
        encoded = base64.b64encode(data).decode()
        result = await self.sandbox.exec(
            f"python3 -I -c {shq(_WRITE_SCRIPT)} {shq(_remote_path(path))} {shq(encoded)}",
            timeout_seconds=30,
            as_runner=True,
        )
        _check(result.exit_code, result.stderr)

    async def list_dir(self, path: str) -> Sequence[FileEntry]:
        remote = _remote_path(path)
        script = (
            "import json,pathlib,sys;"
            "p=pathlib.Path(sys.argv[1]);"
            "print(json.dumps([{'name':x.name,'path':str(x),'is_dir':x.is_dir(),"
            "'size':None if x.is_dir() else x.stat().st_size} for x in sorted(p.iterdir())]))"
        )
        result = await self.sandbox.exec(
            f"python3 -I -c {shq(script)} {shq(remote)}",
            timeout_seconds=30,
            as_runner=True,
        )
        _check(result.exit_code, result.stderr)
        parsed: object = json.loads(result.stdout)
        if not isinstance(parsed, list):
            raise RuntimeError("sandbox directory listing was not a list")
        entries: list[FileEntry] = []
        for item in cast(list[object], parsed):
            if not isinstance(item, dict):
                continue
            record = cast(dict[str, object], item)
            size = record.get("size")
            entries.append(
                FileEntry(
                    name=str(record.get("name", "")),
                    path=str(record.get("path", "")),
                    is_dir=bool(record.get("is_dir")),
                    size=int(size) if isinstance(size, int | float | str) else None,
                )
            )
        return entries

    async def stat(self, path: str) -> FileEntry:
        remote = _remote_path(path)
        script = (
            "import json,pathlib,sys;"
            "p=pathlib.Path(sys.argv[1]);"
            "print(json.dumps({'name':p.name,'path':str(p),'is_dir':p.is_dir(),"
            "'size':None if p.is_dir() else p.stat().st_size}))"
        )
        result = await self.sandbox.exec(
            f"python3 -I -c {shq(script)} {shq(remote)}",
            timeout_seconds=30,
            as_runner=True,
        )
        _check(result.exit_code, result.stderr)
        parsed: object = json.loads(result.stdout)
        if not isinstance(parsed, dict):
            raise RuntimeError("sandbox stat was not an object")
        item = cast(dict[str, object], parsed)
        size = item.get("size")
        return FileEntry(
            name=str(item.get("name", "")),
            path=str(item.get("path", "")),
            is_dir=bool(item.get("is_dir")),
            size=int(size) if isinstance(size, int | float | str) else None,
        )

    async def exists(self, path: str) -> bool:
        result = await self.sandbox.exec(f"test -e {shq(_remote_path(path))}", timeout_seconds=30, as_runner=True)
        return result.exit_code == 0

    async def make_dir(self, path: str) -> None:
        result = await self.sandbox.exec(f"mkdir -p -- {shq(_remote_path(path))}", timeout_seconds=30, as_runner=True)
        _check(result.exit_code, result.stderr)

    async def remove(self, path: str) -> None:
        result = await self.sandbox.exec(f"rm -rf -- {shq(_remote_path(path))}", timeout_seconds=30, as_runner=True)
        _check(result.exit_code, result.stderr)

    async def realpath(self, path: str) -> str:
        result = await self.sandbox.exec(
            f"realpath -m -- {shq(_remote_path(path))}",
            timeout_seconds=30,
            as_runner=True,
        )
        _check(result.exit_code, result.stderr)
        resolved = result.stdout.strip()
        _remote_path(resolved)
        return resolved


def _remote_path(path: str) -> str:
    candidate = path if path.startswith("/") else posixpath.join(REPO_DIR, path)
    normalized = posixpath.normpath(candidate)
    if normalized != REPO_DIR and not normalized.startswith(f"{REPO_DIR}/"):
        raise ValueError(f"path escapes the repository: {path!r}")
    return normalized


def _check(exit_code: int, stderr: str) -> None:
    if exit_code != 0:
        raise OSError(stderr.strip() or f"sandbox command exited {exit_code}")


_READ_SCRIPT = (
    "import base64,pathlib,sys;sys.stdout.write(base64.b64encode(pathlib.Path(sys.argv[1]).read_bytes()).decode())"
)
_WRITE_SCRIPT = (
    "import base64,pathlib,sys;"
    "p=pathlib.Path(sys.argv[1]);p.parent.mkdir(parents=True,exist_ok=True);"
    "p.write_bytes(base64.b64decode(sys.argv[2]))"
)
