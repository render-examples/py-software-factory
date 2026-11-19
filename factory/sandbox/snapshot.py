"""Snapshot naming, construction, pruning, and hash-checked runner payloads."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import re
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast
from urllib.parse import quote

from render.experimental.sandbox.errors import SnapshotNotFoundError

from factory.config import RepoContext
from factory.github.auth import github_token
from factory.github.client import github_for
from factory.github.request import encode_path
from factory.identifiers import is_safe_branch
from factory.sandbox.client import RenderSandbox, SandboxClient
from factory.sandbox.egress import builder_egress, egress_hint
from factory.sandbox.operations import checked, configured_group_id, require_success
from factory.sandbox.shell import shq
from factory.sandbox.types import (
    APP_DIR,
    REPO_DIR,
    RUN_AS,
    RUNNER_HOME,
    RUNNER_PATH,
    RUNNER_TMP,
    SANDBOX_ROOT,
    SCRATCH_DIR,
    SnapshotManifest,
)

SHA1_RE: Final = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
KEEP_SNAPSHOTS: Final = 2
BUILDER_TIMEOUT_SECONDS: Final = 60 * 60
STAGING_DIR: Final = "/tmp/factory-stage"  # noqa: S108
STAGING_CREDS: Final = f"{STAGING_DIR}/creds"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPTS_DIR: Final = PROJECT_ROOT / "sandbox"
RUNTIME_SCRIPTS: Final = (
    "runner-requirements.txt",
    "install-tools.sh",
    "as-runner.sh",
    "run-agent.sh",
    "detach.sh",
    "collect-changes.sh",
    "lockfile-hash.sh",
    "verify.sh",
    "build/git-credential-staged.sh",
    "build/scrub-credentials.sh",
)
GITCONFIG: Final = "[core]\n\tfsmonitor = false\n[advice]\n\tdetachedHead = false\n"

TokenReader = Callable[[RepoContext], Awaitable[str]]


@dataclass(frozen=True)
class SnapshotDeps:
    read_token: TokenReader | None = None


@dataclass(frozen=True)
class BuiltSnapshot:
    id: str
    sandbox_group_id: str
    name: str
    manifest: SnapshotManifest


@dataclass(frozen=True)
class RunnerBundle:
    data: bytes
    sha256: str


def snapshot_name(repo: RepoContext) -> str:
    """The reusable snapshot name for one configured repository."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", f"factory-{repo.owner}-{repo.name}")


def credential_file(token: str) -> bytes:
    if not token or "\n" in token or "\r" in token:
        raise ValueError("GitHub token must be one non-empty line")
    return f"x-access-token\n{token}".encode()


def credential_helper_args(directory: str) -> tuple[str, ...]:
    helper = f"!bash {APP_DIR}/build/git-credential-staged.sh {directory}"
    return ("-c", "credential.helper=", "-c", f"credential.helper={helper}")


async def read_token(repo: RepoContext) -> str:
    return await github_token(repo.installation_id, repo.name, {"contents": "read"})


async def resolve_base_head(repo: RepoContext) -> str:
    """Read the configured branch tip from GitHub's API, not repository-controlled git output."""
    if not is_safe_branch(repo.config.base_branch):
        raise ValueError(f"{repo.full_name}: unsupported base branch {repo.config.base_branch!r}")
    github = github_for(repo)
    try:
        raw = await github.http.json(
            "GET",
            f"/repos/{quote(repo.owner, safe='')}/{quote(repo.name, safe='')}/git/ref/"
            f"heads/{encode_path(repo.config.base_branch)}",
        )
    finally:
        await github.aclose()
    sha: object = None
    if isinstance(raw, dict):
        record = cast(dict[str, object], raw)
        target = record.get("object")
        if isinstance(target, dict):
            sha = cast(dict[str, object], target).get("sha")
    if not isinstance(sha, str) or not SHA1_RE.fullmatch(sha):
        raise RuntimeError(f"GitHub returned no commit for {repo.full_name}:{repo.config.base_branch}")
    return sha


def build_runner_bundle(root: Path = PROJECT_ROOT) -> RunnerBundle:
    """Create a deterministic zip importable through PYTHONPATH.

    The runner package lands in Milestone B; Milestone A still packages the agent
    and schema modules, so this seam can be tested before the entry point exists.
    """
    files: dict[str, bytes] = {}
    factory_init = root / "factory" / "__init__.py"
    files["factory/__init__.py"] = factory_init.read_bytes() if factory_init.is_file() else b""
    identifiers = root / "factory" / "identifiers.py"
    files["factory/identifiers.py"] = identifiers.read_bytes()
    for package in ("runner", "agents", "schemas"):
        base = root / "factory" / package
        if base.is_dir():
            for path in base.rglob("*"):
                if "__pycache__" not in path.parts and path.is_file() and path.suffix in {".py", ".md"}:
                    files[path.relative_to(root).as_posix()] = path.read_bytes()
    # Importing a submodule executes its package __init__. The host packages
    # intentionally re-export host-only lifecycle code, so the runner archive
    # uses narrow package initializers and includes only the shared sandbox type.
    files["factory/agents/__init__.py"] = b""
    files["factory/sandbox/__init__.py"] = b""
    files["factory/sandbox/types.py"] = (root / "factory" / "sandbox" / "types.py").read_bytes()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    data = output.getvalue()
    return RunnerBundle(data, hashlib.sha256(data).hexdigest())


async def ensure_runner_bundle(sandbox: RenderSandbox, bundle: RunnerBundle | None = None) -> bool:
    """Atomically replace the uploaded runner zip after checking its hash in the sandbox."""
    payload = bundle or await asyncio.to_thread(build_runner_bundle)
    control_timeout = sandbox.client.timings.control_timeout_seconds
    current = await sandbox.exec(
        f"cat {shq(f'{APP_DIR}/runner.sha256')} 2>/dev/null",
        timeout_seconds=control_timeout,
    )
    if current.stdout.strip() == payload.sha256:
        return False
    stage = f"/tmp/factory-runner-{uuid.uuid4().hex}"  # noqa: S108
    archive = f"{stage}/runner.zip"
    await checked(sandbox, f"mkdir -m 700 {shq(stage)}", timeout_seconds=control_timeout)
    try:
        await sandbox.upload(archive, payload.data)
        command = " && ".join(
            (
                f"[ \"$(sha256sum {shq(archive)} | cut -d' ' -f1)\" = {shq(payload.sha256)} ]",
                f"install -o root -g root -m 0444 {shq(archive)} {shq(f'{APP_DIR}/runner.zip.next')}",
                f"mv {shq(f'{APP_DIR}/runner.zip.next')} {shq(f'{APP_DIR}/runner.zip')}",
                f"printf '%s\\n' {shq(payload.sha256)} > {shq(f'{APP_DIR}/runner.sha256.next')}",
                f"chmod 0444 {shq(f'{APP_DIR}/runner.sha256.next')}",
                f"mv {shq(f'{APP_DIR}/runner.sha256.next')} {shq(f'{APP_DIR}/runner.sha256')}",
            )
        )
        await checked(
            sandbox,
            command,
            timeout_seconds=control_timeout,
            operation="runner bundle install",
        )
    finally:
        await sandbox.exec(f"rm -rf -- {shq(stage)}", timeout_seconds=control_timeout)
    return True


async def build_snapshot(
    client: SandboxClient,
    repo: RepoContext,
    sha: str,
    *,
    sandbox_group_id: str | None = None,
    deps: SnapshotDeps | None = None,
) -> BuiltSnapshot:
    """Build a credential-scrubbed filesystem snapshot and keep its newest two versions."""
    if not SHA1_RE.fullmatch(sha):
        raise ValueError(f"refusing to build {repo.full_name} at {sha!r}: not a commit SHA")
    branch = repo.config.base_branch
    if not is_safe_branch(branch):
        raise ValueError(f"{repo.full_name}: unsupported base branch {branch!r}")
    group_id = sandbox_group_id or configured_group_id()
    policy = builder_egress(repo)
    created = await client.create(
        name=f"builder-{snapshot_name(repo)}",
        timeout_seconds=BUILDER_TIMEOUT_SECONDS,
        idle_timeout_seconds=0,
        network_policy=policy,
        sandbox_group_id=group_id,
    )
    sandbox = RenderSandbox(created.id, client)
    control_timeout = client.timings.control_timeout_seconds
    token_reader = (deps or SnapshotDeps()).read_token or read_token
    try:
        await client.wait_running(sandbox.id)
        await _prepare_runtime(sandbox)
        token = await token_reader(repo)
        await sandbox.exec(f"mkdir -p -m 700 {shq(STAGING_CREDS)}")
        await sandbox.upload(f"{STAGING_CREDS}/github.com", credential_file(token))
        await _clone_and_scrub(sandbox, repo, sha)
        await checked(
            sandbox,
            f"chown -R {shq(RUN_AS)}: {shq(REPO_DIR)}",
            timeout_seconds=control_timeout,
        )
        lockfile_hash = await compute_lockfile_hash(sandbox)
        install = repo.config.install.strip() if repo.config.install and repo.config.install.strip() else None
        if install:
            await run_install(sandbox, install, egress_hint(repo, policy))
        manifest = SnapshotManifest(
            repo=repo.full_name,
            base_branch=branch,
            sha=sha,
            lockfile_hash=lockfile_hash,
            runner_requirements_hash=runner_requirements_hash(),
            install=install,
            created_at=datetime.now(UTC).isoformat(),
        )
        await sandbox.upload(f"{APP_DIR}/manifest.json", _manifest_json(manifest))
        await _lock_down(sandbox)
        verify = await sandbox.exec(
            f"bash {shq(f'{APP_DIR}/verify.sh')}",
            timeout_seconds=client.timings.verify_timeout_seconds,
        )
        require_success(verify, "sandbox verify")
        snapshot = await client.create_snapshot(sandbox.id, snapshot_name(repo))
        await prune_snapshots(client, snapshot.sandbox_group_id, snapshot_name(repo))
        return BuiltSnapshot(snapshot.id, snapshot.sandbox_group_id, snapshot_name(repo), manifest)
    finally:
        await sandbox.close()


async def _prepare_runtime(sandbox: RenderSandbox) -> None:
    control_timeout = sandbox.client.timings.control_timeout_seconds
    await checked(
        sandbox,
        " && ".join(
            (
                f"mkdir -p -m 700 {shq(STAGING_DIR)}",
                f"mkdir -p {shq(APP_DIR)} {shq(f'{APP_DIR}/build')} {shq(REPO_DIR)} {shq(SCRATCH_DIR)}",
            )
        ),
        timeout_seconds=control_timeout,
    )
    for relative in RUNTIME_SCRIPTS:
        await sandbox.upload(f"{APP_DIR}/{relative}", await asyncio.to_thread((SCRIPTS_DIR / relative).read_bytes))
    await checked(
        sandbox,
        f"chmod 0755 {shq(APP_DIR)}/*.sh {shq(f'{APP_DIR}/build')}/*.sh",
        timeout_seconds=control_timeout,
    )
    install = await sandbox.exec(
        f"bash {shq(f'{APP_DIR}/install-tools.sh')}",
        timeout_seconds=sandbox.client.timings.prepare_timeout_seconds,
    )
    require_success(install, "tool install")
    await checked(sandbox, _create_runner_user_command(), timeout_seconds=control_timeout)
    await sandbox.upload(f"{APP_DIR}/env", _env_file())
    await sandbox.upload(f"{APP_DIR}/gitconfig", GITCONFIG.encode())
    await ensure_runner_bundle(sandbox)


async def _clone_and_scrub(sandbox: RenderSandbox, repo: RepoContext, sha: str) -> None:
    control_timeout = sandbox.client.timings.control_timeout_seconds
    url = f"https://github.com/{repo.owner}/{repo.name}.git"
    argv = (
        "git",
        *credential_helper_args(STAGING_CREDS),
        "clone",
        "--quiet",
        "--no-tags",
        "--branch",
        repo.config.base_branch,
        "--single-branch",
        url,
        REPO_DIR,
    )
    clone = await sandbox.exec(
        " ".join(shq(arg) for arg in argv) + f" && git -C {shq(REPO_DIR)} checkout --quiet --detach {shq(sha)}",
        timeout_seconds=sandbox.client.timings.prepare_timeout_seconds,
    )
    clone_error: BaseException | None = None
    try:
        require_success(clone, "repository clone")
    except BaseException as error:
        clone_error = error
    scrub = await sandbox.exec(
        f"bash {shq(f'{APP_DIR}/build/scrub-credentials.sh')} "
        f"{shq(STAGING_CREDS)} {shq(REPO_DIR)} {shq(f'{SCRATCH_DIR}/home')}",
        timeout_seconds=control_timeout,
    )
    require_success(scrub, "credential scrub")
    if clone_error is not None:
        raise clone_error


async def compute_lockfile_hash(sandbox: RenderSandbox) -> str:
    result = await sandbox.exec(
        f"bash {shq(f'{APP_DIR}/lockfile-hash.sh')} .",
        timeout_seconds=sandbox.client.timings.control_timeout_seconds,
        as_runner=True,
    )
    require_success(result, "lockfile hash")
    value = result.stdout.strip()
    if not SHA256_RE.fullmatch(value):
        raise RuntimeError(f"lockfile-hash.sh printed {value[:200]!r}")
    return value


async def run_install(sandbox: RenderSandbox, install: str, failure_hint: str = "") -> None:
    result = await sandbox.exec(
        install,
        timeout_seconds=sandbox.client.timings.prepare_timeout_seconds,
        as_runner=True,
    )
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise RuntimeError(f"install ({install}) exited {result.exit_code}: {detail}{failure_hint}")
    dirty = await sandbox.exec(
        "git status --porcelain --untracked-files=no",
        timeout_seconds=sandbox.client.timings.control_timeout_seconds,
        as_runner=True,
    )
    require_success(dirty, "git status after install")
    if dirty.stdout.strip():
        lines = "\n".join(dirty.stdout.strip().splitlines()[:20])
        raise RuntimeError(f"install ({install}) modified tracked files:\n{lines}")


async def prune_snapshots(
    client: SandboxClient,
    sandbox_group_id: str,
    name: str,
    keep: int = KEEP_SNAPSHOTS,
) -> None:
    """Best-effort pruning; a stale delete must never invalidate a fresh build."""
    if keep < 1:
        raise ValueError("keep must be positive")
    try:
        snapshots = await client.list_snapshots(
            sandbox_group_id,
            statuses=["available", "failed"],
        )
        named = sorted(
            (snapshot for snapshot in snapshots if snapshot.name == name),
            key=lambda snapshot: snapshot.requested_at,
            reverse=True,
        )
        for snapshot in named[keep:]:
            with contextlib.suppress(SnapshotNotFoundError):
                await client.delete_snapshot(sandbox_group_id, snapshot.id)
    except Exception:
        return


def _create_runner_user_command() -> str:
    return (
        f"id -u {shq(RUN_AS)} >/dev/null 2>&1 || "
        f"useradd --system --user-group --no-create-home --home-dir {shq(f'{SCRATCH_DIR}/home')} "
        f"--shell /usr/sbin/nologin {shq(RUN_AS)}"
    )


def _env_file() -> bytes:
    values = (
        f"FACTORY_ROOT={SANDBOX_ROOT}",
        f"FACTORY_APP_DIR={APP_DIR}",
        f"FACTORY_REPO_DIR={REPO_DIR}",
        f"FACTORY_SCRATCH_DIR={SCRATCH_DIR}",
        f"FACTORY_RUN_AS={RUN_AS}",
        f"FACTORY_PYTHON={SANDBOX_ROOT}/venv/bin/python",
        f"FACTORY_PATH={RUNNER_PATH}",
        f"FACTORY_RUNNER_HOME={RUNNER_HOME}",
        f"FACTORY_RUNNER_TMP={RUNNER_TMP}",
    )
    return ("\n".join(f"export {value}" for value in values) + "\n").encode()


async def _lock_down(sandbox: RenderSandbox) -> None:
    command = " && ".join(
        (
            f"chown root:root {shq(SANDBOX_ROOT)}",
            f"chmod 0755 {shq(SANDBOX_ROOT)}",
            f"chown -R root:root {shq(APP_DIR)}",
            f"chmod -R a+rX,go-w {shq(APP_DIR)}",
            f"chown {shq(RUN_AS)}: {shq(SCRATCH_DIR)}",
            f"chmod 0700 {shq(SCRATCH_DIR)}",
            f"rm -rf -- {shq(STAGING_DIR)}",
        )
    )
    await checked(
        sandbox,
        command,
        timeout_seconds=sandbox.client.timings.control_timeout_seconds,
    )


def _manifest_json(manifest: SnapshotManifest) -> bytes:
    raw = asdict(manifest)
    payload = {
        "repo": raw["repo"],
        "baseBranch": raw["base_branch"],
        "sha": raw["sha"],
        "lockfileHash": raw["lockfile_hash"],
        "runnerRequirementsHash": raw["runner_requirements_hash"],
        "install": raw["install"],
        "createdAt": raw["created_at"],
    }
    return (json.dumps(payload, indent=2) + "\n").encode()


def runner_requirements_hash() -> str:
    return hashlib.sha256((SCRIPTS_DIR / "runner-requirements.txt").read_bytes()).hexdigest()
