"""Access to Render Sandboxes and the factory's concrete sandbox adapter."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar, cast

from render.client.errors import ClientError, RateLimitError, ServerError
from render.experimental.sandbox.errors import SandboxNotFoundError
from render.experimental.sandbox.types import (
    NetworkPolicy,
    SandboxExecEvent,
    SandboxExecOutput,
    Snapshot,
    SnapshotList,
)
from render.experimental.sandbox.types import (
    Sandbox as SdkSandbox,
)

from factory.sandbox.shell import shq
from factory.sandbox.types import APP_DIR, ExecResult

_T = TypeVar("_T")
_OUTPUT_LIMIT = 256 * 1024
_STATUS_RE = re.compile(r"\bstatus\s+(\d{3})\b")


class SnapshotApi(Protocol):
    async def create(
        self,
        sandbox_id: str,
        *,
        kind: str | None = None,
        name: str | None = None,
        wait: bool = False,
    ) -> Snapshot: ...

    async def from_id(self, *, sandbox_group_id: str, snapshot_id: str) -> Snapshot: ...

    async def list(
        self,
        *,
        sandbox_group_id: str,
        status: str | list[str] | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> SnapshotList: ...

    async def delete(self, *, sandbox_group_id: str, snapshot_id: str) -> None: ...


class SandboxApi(Protocol):
    """The render 1.8.0 surface used here. Fakes implement this protocol directly."""

    @property
    def snapshots(self) -> SnapshotApi: ...

    async def create(
        self,
        *,
        name: str | None = None,
        plan: str | None = None,
        timeout_seconds: int | None = None,
        idle_timeout_seconds: int | None = None,
        network_policy: NetworkPolicy | str | None = None,
        snapshot_name: str | None = None,
        sandbox_group_id: str | None = None,
    ) -> SdkSandbox: ...

    async def from_id(self, sandbox_id: str) -> SdkSandbox: ...

    def exec(self, sandbox_id: str, command: str) -> AsyncIterator[SandboxExecEvent]: ...

    async def copy_to(self, sandbox_id: str, local_path: str | os.PathLike[str], remote_path: str) -> None: ...

    async def copy_from(self, sandbox_id: str, remote_path: str, local_path: str | os.PathLike[str]) -> str: ...

    async def terminate(self, sandbox_id: str) -> None: ...


Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


@dataclass(frozen=True)
class SandboxTimings:
    """Runtime budgets."""

    control_timeout_seconds: float = 30
    """Timeout for short streaming control commands."""
    streaming_exec_max_seconds: float = 40
    """Longer commands use detached execution to stay below the SDK's 45-second read timeout."""
    retry_base_seconds: float = 2.0
    retry_max_seconds: float = 10.0
    retry_attempts: int = 5
    boot_timeout_seconds: float = 5 * 60
    prepare_timeout_seconds: float = 40 * 60
    snapshot_timeout_seconds: float = 20 * 60
    verify_timeout_seconds: float = 5 * 60


def _error_status(error: BaseException) -> int | None:
    if isinstance(error, RateLimitError):
        return 429
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status
    if isinstance(error, ServerError):
        match = _STATUS_RE.search(str(error))
        return int(match.group(1)) if match else None
    return None


class SandboxClient:
    """Adds bounded exec, retries, and polling to the SDK."""

    def __init__(
        self,
        api: SandboxApi,
        *,
        timings: SandboxTimings | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self.api = api
        self.timings = timings or SandboxTimings()
        if self.timings.control_timeout_seconds <= 0:
            raise ValueError("control timeout must be positive")
        if self.timings.streaming_exec_max_seconds < self.timings.control_timeout_seconds:
            raise ValueError("streaming exec maximum cannot be shorter than the control timeout")
        self._sleep = sleep
        self._clock = clock

    async def call(
        self,
        operation: Callable[[], Awaitable[_T]],
        *,
        retry_statuses: frozenset[int] = frozenset({429, 503}),
        attempts: int | None = None,
    ) -> _T:
        """Make one API call and retry only explicit transient HTTP statuses."""
        limit = attempts or self.timings.retry_attempts
        if limit < 1:
            raise ValueError("attempts must be positive")
        for attempt in range(limit):
            try:
                return await operation()
            except Exception as error:
                if attempt + 1 >= limit or _error_status(error) not in retry_statuses:
                    raise
                delay = min(
                    self.timings.retry_base_seconds * (2**attempt),
                    self.timings.retry_max_seconds,
                )
                await self._sleep(delay)
        raise AssertionError("unreachable")

    async def create(
        self,
        *,
        name: str | None = None,
        plan: str | None = None,
        timeout_seconds: int | None = None,
        idle_timeout_seconds: int | None = None,
        network_policy: NetworkPolicy | str | None = None,
        snapshot_name: str | None = None,
        sandbox_group_id: str | None = None,
    ) -> SdkSandbox:
        return await self.call(
            lambda: self.api.create(
                name=name,
                plan=plan,
                timeout_seconds=timeout_seconds,
                idle_timeout_seconds=idle_timeout_seconds,
                network_policy=network_policy,
                snapshot_name=snapshot_name,
                sandbox_group_id=sandbox_group_id,
            )
        )

    async def wait_running(self, sandbox_id: str) -> SdkSandbox:
        deadline = self._clock() + self.timings.boot_timeout_seconds
        delay = self.timings.retry_base_seconds
        while True:
            try:
                sandbox = await self.call(lambda: self.api.from_id(sandbox_id))
            except SandboxNotFoundError:
                if self._clock() >= deadline:
                    raise
            else:
                if sandbox.status == "running":
                    return sandbox
                if sandbox.status in {"errored", "terminated", "suspended", "suspending"}:
                    raise RuntimeError(f"sandbox {sandbox_id} is {sandbox.status}")
                if self._clock() >= deadline:
                    raise TimeoutError(f"sandbox {sandbox_id} is still {sandbox.status}")
            await self._sleep(delay)
            delay = min(max(delay * 2, self.timings.retry_base_seconds), self.timings.retry_max_seconds)

    async def exec(
        self,
        sandbox_id: str,
        command: str,
        *,
        timeout_seconds: float | None = None,
    ) -> ExecResult:
        timeout = timeout_seconds if timeout_seconds is not None else self.timings.control_timeout_seconds

        async def collect() -> ExecResult:
            stdout = _OutputTail()
            stderr = _OutputTail()
            async for event in self.api.exec(sandbox_id, command):
                if isinstance(event, SandboxExecOutput):
                    (stdout if event.stream == "stdout" else stderr).append(event.data)
                else:
                    return ExecResult(event.exit_code, stdout.text(), stderr.text())
            raise RuntimeError(f"exec in {sandbox_id} ended without an exit event")

        try:
            async with asyncio.timeout(timeout):
                return await self.call(collect)
        except TimeoutError as error:
            raise TimeoutError(f"exec in {sandbox_id} timed out after {timeout:g}s") from error

    async def check(
        self,
        sandbox_id: str,
        command: str,
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        result = await self.exec(sandbox_id, command, timeout_seconds=timeout_seconds)
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout).strip()[-2000:]
            raise RuntimeError(f"command exited {result.exit_code} in {sandbox_id}: {detail}")
        return result.stdout

    async def upload(self, sandbox_id: str, path: str, data: bytes) -> None:
        with tempfile.TemporaryDirectory(prefix="factory-upload-") as directory:
            local = Path(directory, "payload")
            await asyncio.to_thread(local.write_bytes, data)
            await self.call(lambda: self.api.copy_to(sandbox_id, local, path))

    async def download(self, sandbox_id: str, path: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="factory-download-") as directory:
            local = Path(directory, "payload")
            written = await self.call(lambda: self.api.copy_from(sandbox_id, path, local))
            return await asyncio.to_thread(Path(written).read_bytes)

    async def terminate(self, sandbox_id: str) -> None:
        try:
            await self.call(lambda: self.api.terminate(sandbox_id))
        except SandboxNotFoundError:
            return
        except ClientError as error:
            if _error_status(error) != 404:
                raise

    async def create_snapshot(self, sandbox_id: str, name: str) -> Snapshot:
        snapshot = await self.call(lambda: self.api.snapshots.create(sandbox_id, kind="filesystem", name=name))
        deadline = self._clock() + self.timings.snapshot_timeout_seconds
        delay = self.timings.retry_base_seconds
        while snapshot.status == "creating":
            if self._clock() >= deadline:
                raise TimeoutError(f"snapshot {snapshot.id} is still creating")
            await self._sleep(delay)
            delay = min(delay * 2, self.timings.retry_max_seconds)
            group_id, snapshot_id = snapshot.sandbox_group_id, snapshot.id
            snapshot = await self.call(
                lambda group_id=group_id, snapshot_id=snapshot_id: self.api.snapshots.from_id(
                    sandbox_group_id=group_id,
                    snapshot_id=snapshot_id,
                )
            )
        if snapshot.status != "available":
            raise RuntimeError(f"snapshot {snapshot.id} {snapshot.status}: {snapshot.error or 'no error given'}")
        return snapshot

    async def list_snapshots(
        self,
        sandbox_group_id: str,
        *,
        statuses: list[str] | None = None,
    ) -> list[Snapshot]:
        snapshots: list[Snapshot] = []
        cursor: str | None = None
        while True:
            page_cursor = cursor
            page = await self.call(
                lambda page_cursor=page_cursor: self.api.snapshots.list(
                    sandbox_group_id=sandbox_group_id,
                    status=statuses,
                    cursor=page_cursor,
                    limit=100,
                )
            )
            snapshots.extend(page.snapshots)
            if not page.next_cursor:
                return snapshots
            cursor = page.next_cursor

    async def delete_snapshot(self, sandbox_group_id: str, snapshot_id: str) -> None:
        await self.call(
            lambda: self.api.snapshots.delete(
                sandbox_group_id=sandbox_group_id,
                snapshot_id=snapshot_id,
            )
        )


class _OutputTail:
    def __init__(self, limit: int = _OUTPUT_LIMIT) -> None:
        self._limit = limit
        self._data = b""

    def append(self, value: str) -> None:
        self._data = (self._data + value.encode())[-self._limit :]

    def text(self) -> str:
        return self._data.decode(errors="replace")


@dataclass(frozen=True)
class _DetachedExecution:
    sandbox_id: str
    directory: str
    timeout_seconds: float


class RenderSandbox:
    """Concrete implementation of :class:`factory.sandbox.types.Sandbox`."""

    def __init__(self, sandbox_id: str, client: SandboxClient) -> None:
        self._id = sandbox_id
        self.client = client
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def id(self) -> str:
        return self._id

    async def exec(
        self,
        command: str,
        *,
        timeout_seconds: float | None = None,
        as_runner: bool = False,
    ) -> ExecResult:
        timeout = timeout_seconds if timeout_seconds is not None else self.client.timings.control_timeout_seconds
        if timeout <= 0:
            raise ValueError("exec timeout must be positive")
        if as_runner:
            command = f"bash {shq(f'{APP_DIR}/as-runner.sh')} bash -c {shq(command)}"
        if timeout > self.client.timings.streaming_exec_max_seconds:
            return await self._exec_detached(command, timeout_seconds=timeout)
        return await self.client.exec(self.id, command, timeout_seconds=timeout)

    async def upload(self, path: str, data: bytes) -> None:
        await self.client.upload(self.id, path, data)

    async def download(self, path: str) -> bytes:
        return await self.client.download(self.id, path)

    async def _exec_detached(self, command: str, *, timeout_seconds: float) -> ExecResult:
        execution = await self._start_detached(command, timeout_seconds=timeout_seconds)
        return await self._wait_detached(execution)

    async def _start_detached(self, command: str, *, timeout_seconds: float) -> _DetachedExecution:
        if timeout_seconds <= 0:
            raise ValueError("detached timeout must be positive")
        directory = f"/tmp/factory-detached-{uuid.uuid4().hex}"  # noqa: S108
        start = (
            f"bash {shq(f'{APP_DIR}/detach.sh')} {shq(directory)} {shq(str(timeout_seconds))} bash -c {shq(command)}"
        )
        await self.client.check(self.id, start, timeout_seconds=self.client.timings.control_timeout_seconds)
        return _DetachedExecution(self.id, directory, timeout_seconds)

    async def _poll_detached(self, execution: _DetachedExecution) -> ExecResult | None:
        """One request: nothing while the command runs; once it has exited, its status and both output tails."""
        self._check_execution(execution)
        directory = execution.directory
        probe = await self.client.exec(
            self.id,
            f"if [ -f {shq(f'{directory}/exit')} ]; then cat {shq(f'{directory}/exit')}; "
            f"tail -c {_OUTPUT_LIMIT} {shq(f'{directory}/stdout')}; "
            f"tail -c {_OUTPUT_LIMIT} {shq(f'{directory}/stderr')} >&2; else exit 3; fi",
            timeout_seconds=self.client.timings.control_timeout_seconds,
        )
        if probe.exit_code == 3:
            return None
        status, _, stdout = probe.stdout.partition("\n")
        if probe.exit_code != 0 or not status.isdigit():
            raise RuntimeError(f"invalid detached status in {directory}: {probe.stdout[:200]!r}")
        return ExecResult(int(status), stdout, probe.stderr)

    async def _wait_detached(self, execution: _DetachedExecution) -> ExecResult:
        self._check_execution(execution)
        deadline = self.client._clock() + execution.timeout_seconds + 15
        delay = self.client.timings.retry_base_seconds
        try:
            while True:
                result = await self._poll_detached(execution)
                if result is not None:
                    return result
                if self.client._clock() >= deadline:
                    raise TimeoutError(f"detached command in {self.id} did not report completion")
                await self.client._sleep(delay)
                delay = min(delay * 1.5, self.client.timings.retry_max_seconds)
        finally:
            with contextlib.suppress(Exception):
                await self.client.exec(
                    self.id,
                    f"rm -rf -- {shq(execution.directory)}",
                    timeout_seconds=self.client.timings.control_timeout_seconds,
                )

    def _check_execution(self, execution: _DetachedExecution) -> None:
        if execution.sandbox_id != self.id:
            raise ValueError("detached execution belongs to another sandbox")

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            await self.client.terminate(self.id)
            self._closed = True


_shared_client: SandboxClient | None = None


def sandbox_client() -> SandboxClient:
    """Build the process-wide client lazily from Render's standard environment."""
    global _shared_client
    if _shared_client is None:
        from render import RenderAsync

        workspace_id = os.environ.get("RENDER_WORKSPACE_ID", "").strip()
        if not workspace_id.startswith("tea-"):
            raise RuntimeError("RENDER_WORKSPACE_ID must be a tea-… workspace id")
        token = os.environ.get("RENDER_API_KEY", "").strip()
        if not token:
            raise RuntimeError("RENDER_API_KEY is not set")
        api = cast(SandboxApi, RenderAsync(token=token, owner_id=workspace_id).sandboxes)
        _shared_client = SandboxClient(api)
    return _shared_client
