from __future__ import annotations

import contextlib
import json
import logging
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast

import logfire
from render.experimental.sandbox.errors import SnapshotNotFoundError, SnapshotNotReadyError
from render.experimental.sandbox.types import NetworkPolicy

from factory.config import RepoContext, get_config
from factory.identifiers import is_safe_branch
from factory.sandbox.client import RenderSandbox, SandboxClient, sandbox_client
from factory.sandbox.egress import egress_hint, task_egress
from factory.sandbox.errors import SandboxHeadMovedError
from factory.sandbox.operations import checked, configured_group_id, require_success
from factory.sandbox.shell import shq
from factory.sandbox.snapshot import (
    SHA1_RE,
    BuiltSnapshot,
    SnapshotDeps,
    build_snapshot,
    compute_lockfile_hash,
    credential_file,
    credential_helper_args,
    read_token,
    resolve_base_head,
    run_install,
    scripts_hash,
    snapshot_name,
)
from factory.sandbox.types import (
    APP_DIR,
    REPO_DIR,
    RUN_AS,
    SCRATCH_DIR,
    BranchRef,
    PullRef,
    SandboxRef,
    SnapshotManifest,
)

logger = logging.getLogger(__name__)

HeadResolver = Callable[[RepoContext], Awaitable[str]]
SnapshotBuilder = Callable[..., Awaitable[BuiltSnapshot]]


@dataclass(frozen=True)
class OpenSandboxDeps:
    read_token: Callable[[RepoContext], Awaitable[str]] | None = None
    resolve_base_head: HeadResolver | None = None
    build_snapshot: SnapshotBuilder | None = None


class OpenedRenderSandbox(RenderSandbox):
    def __init__(
        self,
        sandbox_id: str,
        client: SandboxClient,
        head_sha: str,
        manifest: SnapshotManifest,
    ) -> None:
        super().__init__(sandbox_id, client)
        self.head_sha = head_sha
        self.manifest = manifest


async def open_sandbox(
    repo: RepoContext,
    ref: SandboxRef,
    *,
    client: SandboxClient | None = None,
    sandbox_group_id: str | None = None,
    deps: OpenSandboxDeps | None = None,
) -> OpenedRenderSandbox:
    _check_ref(ref)
    target = f"refs/heads/{ref.name}" if isinstance(ref, BranchRef) else f"pull/{ref.number}@{ref.head_sha}"
    with logfire.span("open sandbox for {repo} at {ref}", repo=repo.full_name, ref=target) as span:
        opened = await _open(repo, ref, client, sandbox_group_id, deps)
        span.set_attributes({"sandbox_id": opened.id, "head_sha": opened.head_sha})
        return opened


async def _open(
    repo: RepoContext,
    ref: SandboxRef,
    client: SandboxClient | None,
    sandbox_group_id: str | None,
    deps: OpenSandboxDeps | None,
) -> OpenedRenderSandbox:
    runtime = client or sandbox_client()
    dependencies = deps or OpenSandboxDeps()
    group_id = sandbox_group_id or configured_group_id()
    policy = task_egress(repo)
    rebuilt = False
    while True:
        sandbox_id = await _create_from_snapshot(runtime, repo, group_id, policy, dependencies)
        sandbox = RenderSandbox(sandbox_id, runtime)
        try:
            await runtime.wait_running(sandbox.id)
            manifest = _parse_manifest(await sandbox.download(f"{APP_DIR}/manifest.json"))
            if manifest.repo != repo.full_name:
                raise RuntimeError(f"snapshot in {sandbox.id} is for {manifest.repo}, not {repo.full_name}")
            if manifest.scripts_hash != scripts_hash():
                if rebuilt:
                    raise RuntimeError(f"the rebuilt snapshot in {sandbox.id} still has other factory scripts")
                await sandbox.close()
                logfire.info("snapshot was built with other factory scripts; rebuilding it")
                await _build(runtime, repo, group_id, dependencies)
                rebuilt = True
                continue
            verify = await sandbox.exec(
                f"bash {shq(f'{APP_DIR}/verify.sh')}",
                timeout_seconds=runtime.timings.verify_timeout_seconds,
            )
            require_success(verify, "sandbox verify")
            head_sha = await _fetch_ref(sandbox, repo, ref, dependencies)
            install = repo.config.install.strip() if repo.config.install and repo.config.install.strip() else None
            if install:
                lockfile_hash = await compute_lockfile_hash(sandbox)
                if lockfile_hash != manifest.lockfile_hash or install != manifest.install:
                    await run_install(sandbox, install, egress_hint(repo, policy))
            return OpenedRenderSandbox(sandbox.id, runtime, head_sha, manifest)
        except BaseException:
            with contextlib.suppress(Exception):
                await sandbox.close()
            raise


@asynccontextmanager
async def with_sandbox(
    repo: RepoContext,
    ref: SandboxRef,
    *,
    client: SandboxClient | None = None,
    sandbox_group_id: str | None = None,
    deps: OpenSandboxDeps | None = None,
) -> AsyncGenerator[OpenedRenderSandbox]:
    sandbox = await open_sandbox(
        repo,
        ref,
        client=client,
        sandbox_group_id=sandbox_group_id,
        deps=deps,
    )
    try:
        yield sandbox
    finally:
        try:
            await sandbox.close()
        except Exception:
            logger.warning("could not close sandbox %s", sandbox.id, exc_info=True)


async def _create_from_snapshot(
    client: SandboxClient,
    repo: RepoContext,
    sandbox_group_id: str,
    policy: NetworkPolicy,
    deps: OpenSandboxDeps,
) -> str:
    async def create() -> str:
        made = await client.create(
            name=f"task-{snapshot_name(repo)}",
            snapshot_name=snapshot_name(repo),
            timeout_seconds=get_config().sandbox.timeout_seconds,
            idle_timeout_seconds=0,
            network_policy=policy,
            sandbox_group_id=sandbox_group_id,
        )
        return made.id

    try:
        return await create()
    except (SnapshotNotFoundError, SnapshotNotReadyError):
        pass
    await _build(client, repo, sandbox_group_id, deps)
    try:
        return await create()
    except (SnapshotNotFoundError, SnapshotNotReadyError) as error:
        raise RuntimeError(
            f"no available snapshot named {snapshot_name(repo)} even after building one inline"
        ) from error


async def _build(client: SandboxClient, repo: RepoContext, sandbox_group_id: str, deps: OpenSandboxDeps) -> None:
    sha = await (deps.resolve_base_head or resolve_base_head)(repo)
    await (deps.build_snapshot or build_snapshot)(
        client,
        repo,
        sha,
        sandbox_group_id=sandbox_group_id,
        deps=SnapshotDeps(read_token=deps.read_token),
    )


async def _fetch_ref(
    sandbox: RenderSandbox,
    repo: RepoContext,
    ref: SandboxRef,
    deps: OpenSandboxDeps,
) -> str:
    directory = f"/tmp/factory-fetch-{uuid.uuid4().hex}"  # noqa: S108
    source = f"refs/heads/{ref.name}" if isinstance(ref, BranchRef) else f"refs/pull/{ref.number}/head"
    refspecs = [source]
    if isinstance(ref, PullRef) and ref.base_branch:
        refspecs.append(f"+refs/heads/{ref.base_branch}:refs/remotes/origin/{ref.base_branch}")
    control_timeout = sandbox.client.timings.control_timeout_seconds
    await checked(sandbox, f"mkdir -m 700 {shq(directory)}", timeout_seconds=control_timeout)
    fetch_error: BaseException | None = None
    try:
        token = await (deps.read_token or read_token)(repo)
        await sandbox.upload(f"{directory}/github.com", credential_file(token))
        await checked(
            sandbox,
            f"chown -R {shq(RUN_AS)}: {shq(directory)} && chmod 0600 {shq(f'{directory}/github.com')}",
            timeout_seconds=control_timeout,
        )
        command = " ".join(
            shq(arg)
            for arg in (
                "git",
                *credential_helper_args(directory),
                "fetch",
                "--quiet",
                "--no-tags",
                "origin",
                *refspecs,
            )
        )
        fetch = await sandbox.exec(
            command,
            timeout_seconds=sandbox.client.timings.prepare_timeout_seconds,
            as_runner=True,
        )
        require_success(fetch, "git fetch")
    except BaseException as error:
        fetch_error = error
    scrub = await sandbox.exec(
        f"bash {shq(f'{APP_DIR}/build/scrub-credentials.sh')} "
        f"{shq(directory)} {shq(REPO_DIR)} {shq(f'{SCRATCH_DIR}/home')}",
        timeout_seconds=control_timeout,
    )
    require_success(scrub, "credential scrub")
    if fetch_error is not None:
        raise fetch_error
    fetched = await sandbox.exec(
        "git rev-parse 'FETCH_HEAD^{commit}'",
        timeout_seconds=control_timeout,
        as_runner=True,
    )
    require_success(fetched, "resolve FETCH_HEAD")
    sha = fetched.stdout.strip()
    if not SHA1_RE.fullmatch(sha):
        raise RuntimeError(f"fetching {source} gave {sha[:100]!r}")
    if isinstance(ref, PullRef) and sha != ref.head_sha:
        raise SandboxHeadMovedError(repo.full_name, ref.number, sha, ref.head_sha)
    checkout = await sandbox.exec(
        f"git checkout --quiet --detach {shq(sha)}",
        timeout_seconds=control_timeout,
        as_runner=True,
    )
    require_success(checkout, "git checkout")
    return sha


def _check_ref(ref: SandboxRef) -> None:
    if isinstance(ref, BranchRef):
        if not is_safe_branch(ref.name):
            raise ValueError(f"unsupported branch name {ref.name!r}")
        return
    if isinstance(ref.number, bool) or ref.number <= 0:
        raise ValueError(f"invalid pull request number {ref.number}")
    if not SHA1_RE.fullmatch(ref.head_sha):
        raise ValueError(f"invalid head SHA {ref.head_sha!r}")
    if ref.base_branch is not None and not is_safe_branch(ref.base_branch):
        raise ValueError(f"unsupported base branch name {ref.base_branch!r}")


def _parse_manifest(data: bytes) -> SnapshotManifest:
    try:
        raw = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("snapshot manifest is not valid JSON") from error
    if not isinstance(raw, dict):
        raise RuntimeError("snapshot manifest must be an object")
    manifest = cast(dict[str, object], raw)

    def text(name: str) -> str:
        value = manifest.get(name)
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"snapshot manifest {name} must be a non-empty string")
        return value

    sha = text("sha")
    lockfile_hash = text("lockfileHash")
    scripts = manifest.get("scriptsHash", "")
    if not SHA1_RE.fullmatch(sha):
        raise RuntimeError("snapshot manifest sha is invalid")
    if not re_full_sha256(lockfile_hash) or not isinstance(scripts, str) or (scripts and not re_full_sha256(scripts)):
        raise RuntimeError("snapshot manifest hash is invalid")
    install = manifest.get("install")
    if install is not None and not isinstance(install, str):
        raise RuntimeError("snapshot manifest install must be a string or null")
    return SnapshotManifest(
        repo=text("repo"),
        base_branch=text("baseBranch"),
        sha=sha,
        lockfile_hash=lockfile_hash,
        scripts_hash=scripts,
        install=install,
        created_at=text("createdAt"),
    )


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
