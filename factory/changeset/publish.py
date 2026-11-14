"""Publishing a sandbox's working tree as one commit, through the Git Data API, with a token that never enters the
sandbox."""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field

from factory.changeset.changes import (
    ChangedFile,
    CheckedChanges,
    PublishLimits,
    Refused,
    check_changes,
    parse_changes_file,
)
from factory.config import RepoContext, get_config
from factory.github import GitDataClient, GitHubError, GitIdentity, TreeEntry, git_data
from factory.sandbox import APP_DIR, REPO_DIR, SAFE_REF, Sandbox, shq

COLLECT_CHANGES_SCRIPT = f"{APP_DIR}/collect-changes.sh"
"""`bash collect-changes.sh <repo_dir> <base_sha> <out_file> <max_changed_files> <max_file_bytes>`; see the
script."""

DEFAULT_IDENTITY = GitIdentity(name="software-factory[bot]", email="software-factory[bot]@users.noreply.github.com")

_SHA = re.compile(r"^[0-9a-f]{40}$")
_COLLECT_TIMEOUT_SECONDS = 10 * 60
_PER_FILE_OVERHEAD = 4096 + 300
"""Slack per changed file in the output file for its diff record, size line, and blob header (paths cap at
4096)."""


@dataclass(frozen=True)
class PublishPaths:
    """Test seams: where the script and checkout live, and where the output goes."""

    script_path: str = COLLECT_CHANGES_SCRIPT
    repo_dir: str = REPO_DIR
    out_path: str | None = None


@dataclass(frozen=True)
class PublishInput:
    sandbox: Sandbox
    repo: RepoContext
    base_sha: str
    """The commit the run started from; the branch's only parent. From trusted input, never model output."""
    branch: str
    message: str
    author: GitIdentity = DEFAULT_IDENTITY
    """Author and committer. Pass a fixed `date` to make retries produce the same commit."""
    git: GitDataClient | None = None
    limits: PublishLimits | None = None
    """Defaults to the configured limits."""
    owned_commits: Collection[str] = field(default_factory=tuple[str, ...])
    """Commits an earlier attempt of this run created: the branch may be moved off them as well as off
    base_sha."""


@dataclass(frozen=True)
class Published:
    commit_sha: str
    tree_sha: str
    changed_files: list[ChangedFile]


async def publish(input: PublishInput, paths: PublishPaths | None = None) -> Published | Refused:
    """Publishes the sandbox's working tree as one commit on `refs/heads/<branch>`.

    Returns a refusal when the change breaks a publishing rule, and raises on any verification mismatch, before a
    ref moves.
    """
    paths = paths or PublishPaths()
    if not _SHA.fullmatch(input.base_sha):
        raise ValueError(f"Invalid base SHA: {input.base_sha}")
    if not is_safe_branch(input.branch):
        raise ValueError(f"Invalid branch name: {input.branch}")
    if not input.message.strip():
        raise ValueError("Commit message is empty")
    if input.limits is not None:
        limits = input.limits
    else:
        configured = get_config().limits
        limits = PublishLimits(configured.max_changed_files, configured.max_file_bytes)

    file = await _collect(input.sandbox, input.base_sha, limits, paths)
    checked = check_changes(parse_changes_file(file), limits)
    if isinstance(checked, Refused):
        return checked

    git = input.git or git_data(input.repo)
    try:
        return await _write(git, input, checked)
    finally:
        if input.git is None:
            await git.aclose()


async def _write(git: GitDataClient, input: PublishInput, checked: CheckedChanges) -> Published:
    base = await git.get_commit(input.base_sha)

    for sha, content in checked.blobs.items():
        created = await git.create_blob(content)
        if created != sha:
            raise RuntimeError(f"GitHub stored blob {created}, expected {sha}")

    tree_sha = await git.create_tree(base.tree_sha, _tree_entries(checked.files))
    if tree_sha != checked.tree_sha:
        raise RuntimeError(f"Tree mismatch: GitHub built {tree_sha}, the sandbox staged {checked.tree_sha}")

    commit_sha = await git.create_commit(
        message=input.message,
        tree=tree_sha,
        parents=[input.base_sha],
        author=input.author,
        committer=input.author,
    )
    await _move_branch(git, input.branch, commit_sha, {input.base_sha}, set(input.owned_commits))
    return Published(commit_sha=commit_sha, tree_sha=tree_sha, changed_files=checked.files)


async def _collect(sandbox: Sandbox, base_sha: str, limits: PublishLimits, paths: PublishPaths) -> bytes:
    """Runs collect-changes.sh and downloads its one output file, checking its size before and after."""
    out_path = paths.out_path or f"/tmp/software-factory-changes-{uuid.uuid4()}"  # noqa: S108 - inside the sandbox
    command = " ".join(
        [
            "bash",
            shq(paths.script_path),
            shq(paths.repo_dir),
            shq(base_sha),
            shq(out_path),
            str(limits.max_changed_files),
            str(limits.max_file_bytes),
        ]
    )
    result = await sandbox.exec(command, timeout_seconds=_COLLECT_TIMEOUT_SECONDS)
    if result.exit_code != 0:
        raise RuntimeError(f"collect-changes.sh failed (exit {result.exit_code}): {result.stderr.strip()[-500:]}")
    reported = result.stdout.strip()
    if not re.fullmatch(r"\d{1,15}", reported):
        raise RuntimeError(f'collect-changes.sh printed "{reported[:80]}", not a size')
    size = int(reported)
    max_size = limits.max_changed_files * (limits.max_file_bytes + _PER_FILE_OVERHEAD) + 4096
    if size > max_size:
        raise RuntimeError(f"Changes file is {size} bytes, more than the {max_size} the limits allow")

    file = await sandbox.download(out_path)
    if len(file) != size:
        raise RuntimeError(f"Downloaded {len(file)} bytes of a {size}-byte changes file")
    return file


def _tree_entries(files: list[ChangedFile]) -> list[TreeEntry]:
    """Deletions go first: GitHub applies entries in order, so turning a file into a directory (or back) needs the
    old entry gone before the new one lands."""
    deletions = sorted((f for f in files if f.sha is None), key=lambda f: f.path.encode())
    writes = sorted((f for f in files if f.sha is not None), key=lambda f: f.path.encode())
    entries = [TreeEntry(path=f.path, mode="100644", sha=None) for f in deletions]
    for f in writes:
        if f.mode == "100644":
            entries.append(TreeEntry(path=f.path, mode="100644", sha=f.sha))
        elif f.mode == "100755":
            entries.append(TreeEntry(path=f.path, mode="100755", sha=f.sha))
        else:
            raise RuntimeError(f"Refusing to write {f.path} with mode {f.mode}")
    return entries


async def _move_branch(git: GitDataClient, branch: str, commit_sha: str, base: set[str], owned: set[str]) -> None:
    """Points the branch at `commit_sha`: creates it, fast-forwards it from the base, or moves it off a commit this
    run created. Any other target belongs to someone else and is left alone."""
    for _ in range(3):
        current = await git.get_ref(branch)
        if current == commit_sha:
            return
        if current is None:
            try:
                await git.create_ref(branch, commit_sha)
            except GitHubError as error:
                if error.status == 422:
                    continue
                raise
        elif current in base:
            await git.update_ref(branch, commit_sha, force=False)
        elif current in owned:
            await git.update_ref(branch, commit_sha, force=True)
        else:
            raise RuntimeError(
                f"Refusing to move refs/heads/{branch}: it points at {current}, which this run didn't create"
            )
        after = await git.get_ref(branch)
        if after != commit_sha:
            raise RuntimeError(f"refs/heads/{branch} points at {after}, expected {commit_sha}")
        return
    raise RuntimeError(f"refs/heads/{branch} kept changing while publishing")


def is_safe_branch(branch: str) -> bool:
    """A branch name git and GitHub both accept, with nothing that reads as a refspec, option, or revision."""
    return (
        SAFE_REF.fullmatch(branch) is not None
        and len(branch) <= 200
        and ".." not in branch
        and "//" not in branch
        and not branch.endswith(("/", ".", ".lock"))
        and not any(s.startswith(".") for s in branch.split("/"))
    )
