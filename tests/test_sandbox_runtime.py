from __future__ import annotations

import importlib
import os
import shlex
import subprocess
import sys
import zipfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from render.client.errors import RateLimitError, ServerError
from render.experimental.sandbox.errors import SandboxNotFoundError
from render.experimental.sandbox.types import (
    NetworkPolicy,
    SandboxExecEvent,
    SandboxExecExit,
    SandboxExecOutput,
    Snapshot,
    SnapshotList,
)
from render.experimental.sandbox.types import (
    Sandbox as SdkSandbox,
)

from factory.config import RepoContext
from factory.config_model import RepoConfig
from factory.sandbox.client import ApiPacer, PacedSandboxClient, RenderSandbox, SandboxApi, SandboxTimings
from factory.sandbox.egress import builder_egress, task_egress
from factory.sandbox.open import open_sandbox
from factory.sandbox.shell import shq
from factory.sandbox.snapshot import as_runner, build_runner_bundle, credential_file
from factory.sandbox.types import BranchRef, PullRef


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeSnapshots:
    async def create(
        self,
        sandbox_id: str,
        *,
        kind: str | None = None,
        name: str | None = None,
        wait: bool = False,
    ) -> Snapshot:
        raise AssertionError("unexpected snapshot create")

    async def from_id(self, *, sandbox_group_id: str, snapshot_id: str) -> Snapshot:
        raise AssertionError("unexpected snapshot get")

    async def list(
        self,
        *,
        sandbox_group_id: str,
        status: str | list[str] | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> SnapshotList:
        return SnapshotList()

    async def delete(self, *, sandbox_group_id: str, snapshot_id: str) -> None:
        return None


class FakeApi:
    def __init__(self) -> None:
        self.snapshots = FakeSnapshots()
        self.events: list[SandboxExecEvent] = []
        self.event_factory: Callable[[str], list[SandboxExecEvent]] | None = None
        self.commands: list[str] = []
        self.terminate_calls = 0
        self.not_found_on_terminate = False

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
        return cast(SdkSandbox, SimpleNamespace(id="sbx-fake", status="running"))

    async def from_id(self, sandbox_id: str) -> SdkSandbox:
        return cast(SdkSandbox, SimpleNamespace(id=sandbox_id, status="running"))

    def exec(self, sandbox_id: str, command: str) -> AsyncIterator[SandboxExecEvent]:
        self.commands.append(command)

        async def stream() -> AsyncIterator[SandboxExecEvent]:
            events = self.event_factory(command) if self.event_factory else self.events
            for event in events:
                yield event

        return stream()

    async def copy_to(self, sandbox_id: str, local_path: str | Path, remote_path: str) -> None:
        raise AssertionError("unexpected upload")

    async def copy_from(self, sandbox_id: str, remote_path: str, local_path: str | Path) -> str:
        raise AssertionError("unexpected download")

    async def terminate(self, sandbox_id: str) -> None:
        self.terminate_calls += 1
        if self.not_found_on_terminate:
            raise SandboxNotFoundError("gone")


def repo(*, egress: tuple[str, ...] | str = ()) -> RepoContext:
    return RepoContext(
        "acme",
        "widget",
        "acme/widget",
        7,
        RepoConfig(base_branch="main", verify=("pytest",), egress=cast(tuple[str, ...], egress)),
    )


@pytest.mark.asyncio
async def test_process_pacer_reserves_slots_across_callers() -> None:
    clock = FakeClock()
    pacer = ApiPacer(0.6, clock=clock, sleep=clock.sleep)

    await pacer.wait()
    await pacer.wait()
    await pacer.wait()

    assert clock.sleeps == pytest.approx([0.6, 0.6])


@pytest.mark.asyncio
async def test_call_retries_only_429_and_503_with_capped_backoff() -> None:
    clock = FakeClock()
    client = PacedSandboxClient(
        cast(SandboxApi, FakeApi()),
        timings=SandboxTimings(
            min_request_interval_seconds=0,
            retry_base_seconds=2,
            retry_max_seconds=3,
            retry_attempts=4,
        ),
        pacer=ApiPacer(0, clock=clock, sleep=clock.sleep),
        sleep=clock.sleep,
        clock=clock,
    )
    failures: list[BaseException] = [
        RateLimitError("slow down", status_code=429),
        ServerError("request failed with status 503"),
    ]

    async def operation() -> str:
        if failures:
            raise failures.pop(0)
        return "ok"

    assert await client.call(operation) == "ok"
    assert clock.sleeps == [2, 3]

    async def permanent() -> str:
        raise ServerError("request failed with status 500")

    with pytest.raises(ServerError):
        await client.call(permanent)


@pytest.mark.asyncio
async def test_collected_exec_keeps_only_256_kib_tail_per_stream() -> None:
    api = FakeApi()
    api.events = [
        SandboxExecOutput("stdout", "a" * (300 * 1024)),
        SandboxExecOutput("stderr", "b" * (260 * 1024)),
        SandboxExecExit(9),
    ]
    client = PacedSandboxClient(
        cast(SandboxApi, api),
        pacer=ApiPacer(0),
        timings=SandboxTimings(min_request_interval_seconds=0),
    )

    result = await client.exec("sbx-fake", "large")

    assert result.exit_code == 9
    assert result.stdout == "a" * (256 * 1024)
    assert result.stderr == "b" * (256 * 1024)


@pytest.mark.asyncio
async def test_close_is_idempotent_and_missing_sandbox_counts_as_closed() -> None:
    api = FakeApi()
    client = PacedSandboxClient(cast(SandboxApi, api), pacer=ApiPacer(0))
    sandbox = RenderSandbox("sbx-fake", client)

    await sandbox.close()
    await sandbox.close()
    assert api.terminate_calls == 1

    missing_api = FakeApi()
    missing_api.not_found_on_terminate = True
    missing = RenderSandbox(
        "sbx-gone",
        PacedSandboxClient(cast(SandboxApi, missing_api), pacer=ApiPacer(0)),
    )
    await missing.close()
    await missing.close()
    assert missing_api.terminate_calls == 1


@pytest.mark.asyncio
async def test_detached_start_quotes_command_and_poll_collects_result() -> None:
    api = FakeApi()

    def events(command: str) -> list[SandboxExecEvent]:
        if "if [ -f" in command:
            return [SandboxExecOutput("stdout", "7\n"), SandboxExecExit(0)]
        if command.startswith("tail") and command.endswith("/stdout'"):
            return [SandboxExecOutput("stdout", "out\n"), SandboxExecExit(0)]
        if command.startswith("tail") and command.endswith("/stderr'"):
            return [SandboxExecOutput("stdout", "err\n"), SandboxExecExit(0)]
        return [SandboxExecExit(0)]

    api.event_factory = events
    client = PacedSandboxClient(cast(SandboxApi, api), pacer=ApiPacer(0))
    sandbox = RenderSandbox("sbx-fake", client)
    hostile = "printf '%s' okay; touch /tmp/should-not-be-a-host-command"

    execution = await sandbox.start_detached(hostile, timeout_seconds=30)
    result = await sandbox.poll_detached(execution)

    start_argv = shlex.split(api.commands[0])
    assert start_argv[-1] == hostile
    assert result is not None
    assert result.exit_code == 7
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"


def test_egress_uses_only_trusted_hosts_and_builder_has_no_model_host() -> None:
    context = repo(egress=("packages.acme.test",))

    task = task_egress(context, ("api.anthropic.com", "api.anthropic.com"))
    builder = builder_egress(context)

    assert [rule.domain for rule in task.rules] == [
        "github.com",
        "api.anthropic.com",
        "packages.acme.test",
    ]
    assert "api.anthropic.com" not in [rule.domain for rule in builder.rules]
    assert {"pypi.org", "files.pythonhosted.org", "release-assets.githubusercontent.com"} <= {
        rule.domain for rule in builder.rules
    }


def test_allow_all_repo_remains_explicit() -> None:
    config = RepoConfig(base_branch="main", verify=("pytest",), egress="allow-all")
    context = RepoContext("acme", "widget", "acme/widget", 7, config)
    assert task_egress(context, ("api.anthropic.com",)) == NetworkPolicy(type="allow-all")


@pytest.mark.parametrize(
    "ref",
    [
        BranchRef("-c core.sshCommand=evil"),
        BranchRef("main; touch /tmp/pwned"),
        PullRef(0, "a" * 40),
        PullRef(1, "not-a-sha"),
        PullRef(1, "a" * 40, "../main"),
    ],
)
@pytest.mark.asyncio
async def test_untrusted_refs_are_rejected_before_any_api_call(ref: BranchRef | PullRef) -> None:
    api = FakeApi()
    client = PacedSandboxClient(cast(SandboxApi, api), pacer=ApiPacer(0))
    with pytest.raises(ValueError, match=r"unsupported|invalid"):
        await open_sandbox(repo(), ref, client=client, sandbox_group_id="sbg-test")


def test_shell_arguments_and_tokens_stay_out_of_command_syntax() -> None:
    hostile = "name'; touch /tmp/pwned; printf '"
    assert shlex.split(f"command {shq(hostile)}") == ["command", hostile]
    assert shlex.split(as_runner("bash", "-c", hostile))[-1] == hostile
    assert credential_file("secret-token") == b"x-access-token\nsecret-token"
    with pytest.raises(ValueError, match="one non-empty line"):
        credential_file("secret\ninjected")


def test_runner_bundle_is_deterministic_and_scoped(tmp_path: Path) -> None:
    (tmp_path / "factory" / "agents").mkdir(parents=True)
    (tmp_path / "factory" / "schemas").mkdir()
    (tmp_path / "factory" / "sandbox").mkdir()
    (tmp_path / "factory" / "agents" / "a.py").write_text("A = 1\n")
    (tmp_path / "factory" / "schemas" / "s.py").write_text("S = 2\n")
    (tmp_path / "factory" / "sandbox" / "types.py").write_text("ROOT = '/opt/factory'\n")
    (tmp_path / "factory" / "secret.py").write_text("TOKEN = 'do not package'\n")

    first = build_runner_bundle(tmp_path)
    second = build_runner_bundle(tmp_path)

    assert first == second
    assert b"do not package" not in first.data


def test_real_runner_bundle_imports_without_host_only_package_initializers(tmp_path: Path) -> None:
    bundle = build_runner_bundle()
    archive = tmp_path / "runner.zip"
    archive.write_bytes(bundle.data)
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.read("factory/agents/__init__.py") == b""
        assert "factory/sandbox/types.py" in zipped.namelist()
    environment = {**os.environ, "PYTHONPATH": str(archive)}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import factory, factory.runner.main; print(factory.__file__)",
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert str(archive) in result.stdout


@pytest.mark.asyncio
async def test_open_failure_after_creation_terminates_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("factory.sandbox.open")
    api = FakeApi()
    client = PacedSandboxClient(cast(SandboxApi, api), pacer=ApiPacer(0))

    async def fail_bundle(sandbox: RenderSandbox, bundle: object = None) -> bool:
        raise RuntimeError("bad runner bundle")

    monkeypatch.setattr(module, "ensure_runner_bundle", fail_bundle)
    with pytest.raises(RuntimeError, match="bad runner bundle"):
        await module.open_sandbox(repo(), BranchRef("main"), client=client, sandbox_group_id="sbg-test")

    assert api.terminate_calls == 1
