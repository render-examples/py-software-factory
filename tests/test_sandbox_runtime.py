from __future__ import annotations

import importlib
import json
import shlex
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

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
    RenderSandbox,
    SandboxApi,
    SandboxClient,
    SandboxTimings,
)
from factory.sandbox.egress import builder_egress, task_egress
from factory.sandbox.open import OpenSandboxDeps, open_sandbox
from factory.sandbox.shell import shq
from factory.sandbox.snapshot import RUNTIME_SCRIPTS, SCRIPTS_DIR, _env_file, credential_file, scripts_hash
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
async def test_calls_are_not_paced() -> None:
    clock = FakeClock()
    client = SandboxClient(cast(SandboxApi, FakeApi()), clock=clock, sleep=clock.sleep)

    async def operation() -> str:
        return "ok"

    for _ in range(5):
        await client.call(operation)
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_call_retries_only_429_and_503_with_capped_backoff() -> None:
    clock = FakeClock()
    client = SandboxClient(
        cast(SandboxApi, FakeApi()),
        timings=SandboxTimings(retry_base_seconds=2, retry_max_seconds=3, retry_attempts=4),
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
    client = SandboxClient(cast(SandboxApi, api))

    result = await client.exec("sbx-fake", "large")

    assert result.exit_code == 9
    assert result.stdout == "a" * (256 * 1024)
    assert result.stderr == "b" * (256 * 1024)


@pytest.mark.asyncio
async def test_close_is_idempotent_and_missing_sandbox_counts_as_closed() -> None:
    api = FakeApi()
    client = SandboxClient(cast(SandboxApi, api))
    sandbox = RenderSandbox("sbx-fake", client)

    await sandbox.close()
    await sandbox.close()
    assert api.terminate_calls == 1

    missing_api = FakeApi()
    missing_api.not_found_on_terminate = True
    missing = RenderSandbox(
        "sbx-gone",
        SandboxClient(cast(SandboxApi, missing_api)),
    )
    await missing.close()
    await missing.close()
    assert missing_api.terminate_calls == 1


@pytest.mark.asyncio
async def test_short_exec_uses_sdk_stream() -> None:
    api = FakeApi()
    api.events = [SandboxExecOutput("stdout", "ok\n"), SandboxExecExit(0)]
    client = SandboxClient(cast(SandboxApi, api))
    sandbox = RenderSandbox("sbx-fake", client)

    result = await sandbox.exec("printf ok", timeout_seconds=30)

    assert result == ExecResult(0, "ok\n", "")
    assert api.commands == ["printf ok"]


@pytest.mark.asyncio
async def test_long_exec_uses_private_detached_transport_and_quotes_command() -> None:
    api = FakeApi()
    detached: list[Path] = []

    def events(command: str) -> list[SandboxExecEvent]:
        if "detach.sh" in command:
            directory = Path(shlex.split(command)[2])
            directory.mkdir()
            (directory / "stdout").write_text("out\nmore\n")
            (directory / "stderr").write_text("err\n")
            (directory / "exit").write_text("7\n")
            detached.append(directory)
            return [SandboxExecExit(0)]
        result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)
        return [
            SandboxExecOutput("stdout", result.stdout),
            SandboxExecOutput("stderr", result.stderr),
            SandboxExecExit(result.returncode),
        ]

    api.event_factory = events
    client = SandboxClient(cast(SandboxApi, api))
    sandbox = RenderSandbox("sbx-fake", client)
    hostile = "printf '%s' okay; touch /tmp/should-not-be-a-host-command"

    result = await sandbox.exec(hostile, timeout_seconds=60)

    start_argv = shlex.split(api.commands[0])
    assert start_argv[-1] == hostile
    assert result == ExecResult(7, "out\nmore\n", "err\n")
    assert hostile not in api.commands[1:]
    # Start, one poll that returns the status and both tails, and cleanup.
    assert len(api.commands) == 3
    assert not detached[0].exists()


def test_task_egress_reaches_only_github_and_the_repository_hosts() -> None:
    context = repo(egress=("packages.acme.test",))

    task = task_egress(context)
    builder = builder_egress(context)

    assert [rule.domain for rule in task.rules] == ["github.com", "packages.acme.test"]
    assert {"pypi.org", "files.pythonhosted.org", "release-assets.githubusercontent.com"} <= {
        rule.domain for rule in builder.rules
    }


def test_allow_all_repo_remains_explicit() -> None:
    config = RepoConfig(base_branch="main", verify=("pytest",), egress="allow-all")
    context = RepoContext("acme", "widget", "acme/widget", 7, config)
    assert task_egress(context) == NetworkPolicy(type="allow-all")


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
    client = SandboxClient(cast(SandboxApi, api))
    with pytest.raises(ValueError, match=r"unsupported|invalid"):
        await open_sandbox(repo(), ref, client=client, sandbox_group_id="sbg-test")


def test_shell_arguments_and_tokens_stay_out_of_command_syntax() -> None:
    hostile = "name'; touch /tmp/pwned; printf '"
    assert shlex.split(f"command {shq(hostile)}") == ["command", hostile]
    assert credential_file("secret-token") == b"x-access-token\nsecret-token"
    with pytest.raises(ValueError, match="one non-empty line"):
        credential_file("secret\ninjected")


class SnapshotApi(FakeApi):
    """Serves a manifest per created sandbox, and enough command output for open_sandbox to finish."""

    def __init__(self, scripts: list[str]) -> None:
        super().__init__()
        self.scripts = scripts
        self.created = 0
        self.event_factory = self.events_for

    async def create(self, **kwargs: object) -> SdkSandbox:
        self.created += 1
        return cast(SdkSandbox, SimpleNamespace(id=f"sbx-{self.created}", status="running"))

    def events_for(self, command: str) -> list[SandboxExecEvent]:
        if "rev-parse" in command:
            return [SandboxExecOutput("stdout", "a" * 40 + "\n"), SandboxExecExit(0)]
        if command.startswith("if [ -f"):
            return [SandboxExecOutput("stdout", "0\n"), SandboxExecExit(0)]
        return [SandboxExecExit(0)]

    async def copy_to(self, sandbox_id: str, local_path: str | Path, remote_path: str) -> None:
        return None

    async def copy_from(self, sandbox_id: str, remote_path: str, local_path: str | Path) -> str:
        assert remote_path.endswith("/manifest.json")
        manifest = {
            "repo": "acme/widget",
            "baseBranch": "main",
            "sha": "b" * 40,
            "lockfileHash": "c" * 64,
            "install": None,
            "createdAt": "2026-01-01T00:00:00+00:00",
        }
        scripts = self.scripts[min(self.created, len(self.scripts)) - 1]
        if scripts:
            manifest["scriptsHash"] = scripts
        Path(local_path).write_text(json.dumps(manifest))
        return str(local_path)


def snapshot_deps(builds: list[str]) -> OpenSandboxDeps:
    async def token(_repo: RepoContext) -> str:
        return "token"

    async def head(_repo: RepoContext) -> str:
        return "d" * 40

    async def build(_client: object, _repo: RepoContext, sha: str, **_kwargs: object) -> object:
        builds.append(sha)
        return None

    return OpenSandboxDeps(read_token=token, resolve_base_head=head, build_snapshot=cast(Any, build))


@pytest.mark.asyncio
async def test_a_snapshot_with_other_scripts_is_rebuilt_once_and_reopened() -> None:
    api = SnapshotApi(["", scripts_hash()])
    builds: list[str] = []
    opened = await open_sandbox(
        repo(),
        BranchRef("main"),
        client=SandboxClient(cast(SandboxApi, api)),
        sandbox_group_id="sbg-test",
        deps=snapshot_deps(builds),
    )
    assert opened.id == "sbx-2"
    assert builds == ["d" * 40]
    assert api.terminate_calls == 1


@pytest.mark.asyncio
async def test_a_rebuild_that_is_still_stale_fails_and_cleans_up() -> None:
    api = SnapshotApi(["0" * 64, "0" * 64])
    with pytest.raises(RuntimeError, match="still has other factory scripts"):
        await open_sandbox(
            repo(),
            BranchRef("main"),
            client=SandboxClient(cast(SandboxApi, api)),
            sandbox_group_id="sbg-test",
            deps=snapshot_deps([]),
        )
    assert api.terminate_calls == 2


@pytest.mark.asyncio
async def test_open_failure_after_creation_terminates_sandbox() -> None:
    api = FakeApi()
    with pytest.raises(AssertionError, match="unexpected download"):
        await open_sandbox(
            repo(), BranchRef("main"), client=SandboxClient(cast(SandboxApi, api)), sandbox_group_id="sbg-test"
        )
    assert api.terminate_calls == 1


def test_scripts_hash_covers_every_installed_script() -> None:
    assert "fileops.py" in RUNTIME_SCRIPTS
    assert all((SCRIPTS_DIR / name).is_file() for name in RUNTIME_SCRIPTS)
    assert scripts_hash() == scripts_hash()
