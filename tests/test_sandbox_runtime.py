from __future__ import annotations

import asyncio
import importlib
import os
import re
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
from factory.sandbox.client import (
    ApiPacer,
    PacedSandboxClient,
    RenderSandbox,
    SandboxApi,
    SandboxTimings,
    measure_sandbox_requests,
)
from factory.sandbox.egress import builder_egress, task_egress
from factory.sandbox.open import open_sandbox
from factory.sandbox.shell import shq
from factory.sandbox.snapshot import _env_file, build_runner_bundle, credential_file
from factory.sandbox.types import BranchRef, ExecResult, PullRef


def test_sandbox_environment_exports_values_for_child_processes() -> None:
    lines = _env_file().decode().splitlines()
    assert lines
    assert all(line.startswith("export FACTORY_") for line in lines)
    assert any(line.startswith("export FACTORY_RUN_AS=") for line in lines)


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


def repo(*, base_branch: str = "main", egress: tuple[str, ...] | str = ()) -> RepoContext:
    return RepoContext(
        "acme",
        "widget",
        "acme/widget",
        7,
        RepoConfig(base_branch=base_branch, verify=("pytest",), egress=cast(tuple[str, ...], egress)),
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
async def test_client_builds_default_pacer_from_configured_interval() -> None:
    clock = FakeClock()
    client = PacedSandboxClient(
        cast(SandboxApi, FakeApi()),
        timings=SandboxTimings(min_request_interval_seconds=0.7),
        clock=clock,
        sleep=clock.sleep,
    )

    async def operation() -> str:
        return "ok"

    await client.call(operation)
    await client.call(operation)
    assert clock.sleeps == pytest.approx([0.7])


@pytest.mark.asyncio
async def test_explicit_pacer_override_wins_over_timing_config() -> None:
    clock = FakeClock()
    client = PacedSandboxClient(
        cast(SandboxApi, FakeApi()),
        timings=SandboxTimings(min_request_interval_seconds=9),
        pacer=ApiPacer(0.25, clock=clock, sleep=clock.sleep),
        clock=clock,
        sleep=clock.sleep,
    )

    async def operation() -> str:
        return "ok"

    await client.call(operation)
    await client.call(operation)
    assert clock.sleeps == pytest.approx([0.25])


@pytest.mark.asyncio
async def test_request_meter_isolates_concurrent_agent_contexts() -> None:
    client = PacedSandboxClient(
        cast(SandboxApi, FakeApi()),
        timings=SandboxTimings(min_request_interval_seconds=0),
    )

    async def operation() -> str:
        await asyncio.sleep(0)
        return "ok"

    async def measured(count: int) -> int:
        with measure_sandbox_requests() as meter:
            await asyncio.gather(*(client.call(operation) for _ in range(count)))
            return meter.count

    assert await asyncio.gather(measured(2), measured(3)) == [2, 3]


@pytest.mark.asyncio
async def test_request_meter_counts_each_retry_attempt() -> None:
    client = PacedSandboxClient(
        cast(SandboxApi, FakeApi()),
        timings=SandboxTimings(
            min_request_interval_seconds=0,
            retry_base_seconds=0,
            retry_attempts=3,
        ),
    )
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RateLimitError("slow down", status_code=429)
        return "ok"

    with measure_sandbox_requests() as meter:
        assert await client.call(operation) == "ok"
    assert meter.count == 3


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
async def test_short_exec_uses_sdk_stream() -> None:
    api = FakeApi()
    api.events = [SandboxExecOutput("stdout", "ok\n"), SandboxExecExit(0)]
    client = PacedSandboxClient(cast(SandboxApi, api), pacer=ApiPacer(0))
    sandbox = RenderSandbox("sbx-fake", client)

    result = await sandbox.exec("printf ok", timeout_seconds=30)

    assert result == ExecResult(0, "ok\n", "")
    assert api.commands == ["printf ok"]


@pytest.mark.asyncio
async def test_long_exec_uses_private_detached_transport_and_quotes_command() -> None:
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

    result = await sandbox.exec(hostile, timeout_seconds=60)

    start_argv = shlex.split(api.commands[0])
    assert start_argv[-1] == hostile
    assert result.exit_code == 7
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    assert hostile not in api.commands[1:]


class FollowedFile:
    """Answers following polls from a file that grows by one step per poll, like a detached runner writing it."""

    def __init__(self, sizes: list[int], data: bytes, *, exit_after: int | None) -> None:
        self.sizes = sizes
        self.data = data
        self.exit_after = exit_after
        self.polls = 0
        self.offsets: list[int] = []

    def __call__(self, command: str) -> list[SandboxExecEvent]:
        if "detach.sh" in command or command.startswith("rm -rf"):
            return [SandboxExecExit(0)]
        if command.startswith("tail -c 262144"):
            stream = "out\n" if command.endswith("/stdout'") else "err\n"
            return [SandboxExecOutput("stdout", stream), SandboxExecExit(0)]
        match = re.search(r"tail -c \+(\d+) ", command)
        assert match is not None, command
        assert "| head -c 262144 " in command
        offset = int(match.group(1)) - 1
        self.offsets.append(offset)
        written = self.sizes[min(self.polls, len(self.sizes) - 1)]
        self.polls += 1
        done = self.exit_after is not None and self.polls >= self.exit_after
        chunk = self.data[offset:written][: 256 * 1024]
        events: list[SandboxExecEvent] = [SandboxExecOutput("stderr", "exit=5" if done else "running")]
        if chunk:
            events.insert(0, SandboxExecOutput("stdout", chunk.decode()))
        return [*events, SandboxExecExit(0)]


def following_sandbox(api: FakeApi, clock: FakeClock) -> RenderSandbox:
    client = PacedSandboxClient(
        cast(SandboxApi, api),
        pacer=ApiPacer(0),
        sleep=clock.sleep,
        clock=clock,
    )
    return RenderSandbox("sbx-fake", client)


@pytest.mark.asyncio
async def test_exec_following_reads_the_file_tail_in_each_single_request_poll() -> None:
    data = b"".join(f"line {n}\n".encode() for n in range(10))
    followed = FollowedFile([0, 12, 40, len(data)], data, exit_after=4)
    api = FakeApi()
    api.event_factory = followed
    clock = FakeClock()
    received: list[bytes] = []
    offset = 0

    def on_output(chunk: bytes) -> None:
        nonlocal offset
        received.append(chunk)
        offset += len(chunk)

    result = await following_sandbox(api, clock).exec_following(
        "run it",
        timeout_seconds=600,
        follow_path="/opt/factory/scratch/runs/agent-1.jsonl",
        offset=lambda: offset,
        on_output=on_output,
    )

    assert result == ExecResult(5, "out\n", "err\n")
    assert b"".join(received) == data
    assert followed.offsets == [0, 0, 12, 40]
    polls = [command for command in api.commands if "tail -c +" in command]
    assert len(polls) == followed.polls == 4
    assert all("/opt/factory/scratch/runs/agent-1.jsonl" in command for command in polls)
    assert "detach.sh" in api.commands[0]
    assert api.commands[1:5] == polls
    assert len(api.commands) == 1 + 4 + 2 + 1
    assert api.commands[-1].startswith("rm -rf -- '/tmp/factory-detached-")
    assert clock.sleeps == [2.0, 3.0, 4.5]


@pytest.mark.asyncio
async def test_exec_following_caps_each_poll_at_256_kib_and_resumes_at_the_offset() -> None:
    data = b"x" * (600 * 1024)
    followed = FollowedFile([len(data)], data, exit_after=4)
    api = FakeApi()
    api.event_factory = followed
    received: list[bytes] = []

    await following_sandbox(api, FakeClock()).exec_following(
        "run it",
        timeout_seconds=600,
        follow_path="/tmp/out.jsonl",
        offset=lambda: sum(map(len, received)),
        on_output=received.append,
    )

    assert [len(chunk) for chunk in received] == [256 * 1024, 256 * 1024, 88 * 1024]
    assert followed.offsets == [0, 256 * 1024, 512 * 1024, 600 * 1024]


@pytest.mark.asyncio
async def test_exec_following_backs_off_to_the_cap_and_honors_the_deadline() -> None:
    followed = FollowedFile([0], b"", exit_after=None)
    api = FakeApi()
    api.event_factory = followed
    clock = FakeClock()

    with pytest.raises(TimeoutError, match="did not report completion"):
        await following_sandbox(api, clock).exec_following(
            "never ends",
            timeout_seconds=60,
            follow_path="/tmp/out.jsonl",
            offset=lambda: 0,
            on_output=lambda _chunk: None,
        )

    assert clock.sleeps[:6] == [2.0, 3.0, 4.5, 6.75, 10.0, 10.0]
    assert sum(clock.sleeps[:-1]) < 60 + 15 <= sum(clock.sleeps)
    assert followed.polls == len(clock.sleeps) + 1
    assert api.commands[-1].startswith("rm -rf -- '/tmp/factory-detached-")


@pytest.mark.asyncio
async def test_exec_following_poll_command_frames_status_and_bytes_in_a_real_shell(tmp_path: Path) -> None:
    followed = tmp_path / "run.jsonl"
    followed.write_bytes(b'{"a":1}\n{"b":2}\n')
    detached: list[str] = []
    api = FakeApi()

    def events(command: str) -> list[SandboxExecEvent]:
        if "detach.sh" in command:
            detached.append(shlex.split(command)[2])
            return [SandboxExecExit(0)]
        if "tail -c +" in command:
            Path(detached[0]).mkdir(exist_ok=True)
            Path(detached[0], "exit").write_text("0\n")
        result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)
        return [
            SandboxExecOutput("stdout", result.stdout),
            SandboxExecOutput("stderr", result.stderr),
            SandboxExecExit(result.returncode),
        ]

    api.event_factory = events
    received: list[bytes] = []
    result = await following_sandbox(api, FakeClock()).exec_following(
        "run it",
        timeout_seconds=600,
        follow_path=str(followed),
        offset=lambda: len(b'{"a":1}\n'),
        on_output=received.append,
    )

    assert received == [b'{"b":2}\n']
    assert result.exit_code == 0
    assert not Path(detached[0]).exists()


@pytest.mark.asyncio
async def test_exec_following_rejects_an_unframed_status() -> None:
    api = FakeApi()

    def events(command: str) -> list[SandboxExecEvent]:
        if "tail -c +" in command:
            return [SandboxExecOutput("stderr", "tail: oops\nrunning"), SandboxExecExit(0)]
        return [SandboxExecExit(0)]

    api.event_factory = events
    with pytest.raises(RuntimeError, match="invalid detached status"):
        await following_sandbox(api, FakeClock()).exec_following(
            "run it",
            timeout_seconds=600,
            follow_path="/tmp/out.jsonl",
            offset=lambda: 0,
            on_output=lambda _chunk: None,
        )
    assert api.commands[-1].startswith("rm -rf -- '/tmp/factory-detached-")


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


@pytest.mark.asyncio
async def test_resolve_base_head_preserves_slashes_in_git_ref_path(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("factory.sandbox.snapshot")
    requested: list[str] = []

    class Http:
        async def json(self, method: str, path: str) -> object:
            assert method == "GET"
            requested.append(path)
            return {"object": {"sha": "a" * 40}}

    class GitHub:
        http = Http()

        async def aclose(self) -> None:
            return None

    def fake_github_for(_repo: RepoContext) -> GitHub:
        return GitHub()

    monkeypatch.setattr(module, "github_for", fake_github_for)
    assert await module.resolve_base_head(repo(base_branch="release/2.x")) == "a" * 40
    assert requested == ["/repos/acme/widget/git/ref/heads/release/2.x"]


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
    (tmp_path / "factory" / "identifiers.py").write_text("SAFE = True\n")
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
        assert "factory/identifiers.py" in zipped.namelist()
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
