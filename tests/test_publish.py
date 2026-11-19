from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from factory.changeset import (
    ChangesFileError,
    Published,
    PublishInput,
    PublishLimits,
    PublishPaths,
    Refused,
    blob_sha,
    is_safe_branch,
)
from factory.config import RepoContext
from factory.config_model import RepoConfig
from factory.github import GitDataClient, GitIdentity, git_data
from factory.sandbox import ExecResult

SCRIPT = str(Path(__file__).parent.parent / "sandbox" / "collect-changes.sh")
IDENTITY = GitIdentity(name="Factory Bot", email="bot@example.com", date="2026-10-18T12:00:00Z")
REPO = RepoContext("acme", "widgets", "acme/widgets", 1, RepoConfig(base_branch="main", verify=()))
LIMITS = PublishLimits(max_changed_files=50, max_file_bytes=100_000)
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}
TOKEN = "ghs_contents_write_token"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git_raw(cwd: Path, *args: str, env: dict[str, str] | None = None, input: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env or GIT_ENV, input=input, capture_output=True, check=True
    ).stdout


def git(cwd: Path, *args: str) -> str:
    return git_raw(cwd, *args).decode().strip()


def sha1(data: bytes) -> str:
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def write(root: Path, path: str, data: str | bytes) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data.encode() if isinstance(data, str) else data)


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repository with one base commit whose attributes would change bytes under `git archive`."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, ".gitattributes", "*.txt text eol=crlf\n*.bin binary\n/ignored/** export-ignore\n*.id ident\n")
    write(root, "a.txt", "hello\n")
    write(root, "ignored/keep.txt", "keep\n")
    write(root, "app/s/page.tsx", "s\n")
    write(root, "conv", "file\n")
    write(root, "dir2/x.txt", "x\n")
    write(root, "run.sh", "#!/bin/sh\n")
    write(root, "gone.txt", "bye\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root, git(root, "rev-parse", "HEAD")


@dataclass
class Entry:
    mode: str
    sha: str


def hash_tree(flat: dict[str, Entry]) -> str:
    """Hashes a flat path map as git hashes the nested trees."""
    root: dict[str, Any] = {}
    for path, entry in flat.items():
        *dirs, name = path.split("/")
        node = root
        for d in dirs:
            node = node.setdefault(d, {})
        node[name] = entry

    def hash_node(node: dict[str, Any]) -> str:
        items: list[tuple[bytes, str, bytes, str]] = []
        for name, value in node.items():
            if isinstance(value, dict):
                items.append(((name + "/").encode(), "40000", name.encode(), hash_node(value)))  # pyright: ignore[reportUnknownArgumentType]
            else:
                items.append((name.encode(), value.mode, name.encode(), value.sha))
        items.sort(key=lambda i: i[0])
        body = b"".join(f"{mode} ".encode() + name + b"\0" + bytes.fromhex(sha) for _, mode, name, sha in items)
        return sha1(b"tree %d\0" % len(body) + body)

    return hash_node(root)


Interceptor = Callable[[str, str, dict[str, Any]], httpx.Response | None]


@dataclass
class FakeGitHub:
    """An in-memory Git Data API."""

    blobs: dict[str, bytes] = field(default_factory=dict[str, bytes])
    trees: dict[str, dict[str, Entry]] = field(default_factory=dict[str, dict[str, Entry]])
    commits: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    refs: dict[str, str] = field(default_factory=dict[str, str])
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list[tuple[str, str, dict[str, Any]]])
    tokens: set[str] = field(default_factory=set[str])

    @classmethod
    def from_repo(cls, root: Path, base: str) -> FakeGitHub:
        """Seeds the base commit from the real repository, before any sandbox tampering."""
        gh = cls()
        flat: dict[str, Entry] = {}
        for record in git_raw(root, "ls-tree", "-r", "-z", "--full-tree", base).decode().split("\0"):
            if record:
                meta, path = record.split("\t", 1)
                mode, _, sha = meta.split(" ")
                flat[path] = Entry(mode, sha)
        tree = hash_tree(flat)
        assert tree == git(root, "rev-parse", f"{base}^{{tree}}")
        gh.trees[tree] = flat
        gh.commits[base] = {"tree": tree, "parents": [], "body": None}
        return gh

    def client(self) -> GitDataClient:
        async def token() -> str:
            return TOKEN

        async def sleep(_: float) -> None:
            return None

        return git_data(REPO, token=token, transport=httpx.MockTransport(self.handle), sleep=sleep)

    def files(self, tree: str) -> dict[str, Entry]:
        return self.trees.get(tree, {})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.tokens.add(request.headers["authorization"].removeprefix("Bearer "))
        prefix = "/repos/acme/widgets/git"
        path = request.url.raw_path.decode().split("?")[0]
        if not path.startswith(prefix):
            return httpx.Response(404, json={"message": "Not Found"})
        path = path[len(prefix) :]
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, body))
        return self.route(request.method, path, body)

    def route(self, method: str, path: str, body: dict[str, Any]) -> httpx.Response:
        commit_match = re.fullmatch(r"/commits/([0-9a-f]{40})", path)
        ref_match = re.fullmatch(r"/refs?/heads/(.+)", path)
        branch = "/".join(unquote(s) for s in ref_match.group(1).split("/")) if ref_match else ""
        if method == "GET" and commit_match:
            commit = self.commits.get(commit_match.group(1))
            if commit is None:
                return httpx.Response(404, json={"message": "Not Found"})
            parents = [{"sha": p} for p in commit["parents"]]
            return httpx.Response(
                200, json={"sha": commit_match.group(1), "tree": {"sha": commit["tree"]}, "parents": parents}
            )
        if method == "POST" and path == "/blobs":
            content = base64.b64decode(body["content"])
            sha = blob_sha(content)
            self.blobs[sha] = content
            return httpx.Response(201, json={"sha": sha})
        if method == "POST" and path == "/trees":
            base = self.trees.get(body["base_tree"])
            if base is None:
                return httpx.Response(422, json={"message": "base_tree is not a tree"})
            tree = dict(base)
            for e in body["tree"]:
                if e["sha"] is None:
                    tree.pop(e["path"], None)
                    continue
                if e["sha"] not in self.blobs:
                    return httpx.Response(422, json={"message": f"blob {e['sha']} not found"})
                parts = e["path"].split("/")
                if any("/".join(parts[:i]) in tree for i in range(1, len(parts))):
                    return httpx.Response(422, json={"message": f"{e['path']} is under a file"})
                if any(k.startswith(f"{e['path']}/") for k in tree):
                    return httpx.Response(422, json={"message": f"{e['path']} is a directory"})
                tree[e["path"]] = Entry(e["mode"], e["sha"])
            sha = hash_tree(tree)
            self.trees[sha] = tree
            return httpx.Response(201, json={"sha": sha})
        if method == "POST" and path == "/commits":
            if body["tree"] not in self.trees:
                return httpx.Response(422, json={"message": "tree not found"})
            author: dict[str, Any] = body.get("author") or {}
            seed = body if author.get("date") else {**body, "nonce": secrets.token_hex()}
            sha = sha1(json.dumps(seed, sort_keys=True).encode())
            self.commits[sha] = {"tree": body["tree"], "parents": body.get("parents", []), "body": body}
            return httpx.Response(201, json={"sha": sha})
        if method == "GET" and ref_match and path.startswith("/ref/"):
            sha = self.refs.get(branch)
            if sha is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"ref": f"refs/heads/{branch}", "object": {"sha": sha, "type": "commit"}})
        if method == "POST" and path == "/refs":
            name = body["ref"].removeprefix("refs/heads/")
            if name in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            if body["sha"] not in self.commits:
                return httpx.Response(422, json={"message": "Object does not exist"})
            self.refs[name] = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"], "object": {"sha": body["sha"]}})
        if method == "PATCH" and ref_match and path.startswith("/refs/"):
            current = self.refs.get(branch)
            if current is None:
                return httpx.Response(422, json={"message": "Reference does not exist"})
            if not body.get("force") and not self.is_ancestor(current, body["sha"]):
                return httpx.Response(422, json={"message": "Update is not a fast forward"})
            self.refs[branch] = body["sha"]
            return httpx.Response(200, json={"ref": f"refs/heads/{branch}", "object": {"sha": body["sha"]}})
        return httpx.Response(404, json={"message": f"No route for {method} {path}"})

    def is_ancestor(self, ancestor: str, sha: str) -> bool:
        queue = [sha]
        while queue:
            current = queue.pop()
            if current == ancestor:
                return True
            queue.extend(self.commits.get(current, {}).get("parents", []))
        return False


class LocalSandbox:
    """exec runs the command locally; download reads the local file."""

    id = "sbx-local"

    def __init__(self) -> None:
        self.execs: list[tuple[str, bool]] = []
        self.downloads: list[str] = []
        self.tamper: Callable[[bytes], bytes] | None = None

    async def exec(self, command: str, *, timeout_seconds: float | None = None, as_runner: bool = False) -> ExecResult:
        self.execs.append((command, as_runner))
        result = subprocess.run(["bash", "-c", command], capture_output=True, check=False, timeout=timeout_seconds)
        return ExecResult(result.returncode, result.stdout.decode(), result.stderr.decode())

    async def upload(self, path: str, data: bytes) -> None:
        return None

    async def download(self, path: str) -> bytes:
        self.downloads.append(path)
        data = Path(path).read_bytes()
        return self.tamper(data) if self.tamper else data

    async def close(self) -> None:
        return None


@dataclass
class Harness:
    root: Path
    base: str
    tmp: Path
    gh: FakeGitHub
    sandbox: LocalSandbox = field(default_factory=LocalSandbox)
    out_path: Path | None = None
    runs: int = 0

    async def run(self, **overrides: Any) -> Published | Refused:
        self.runs += 1
        self.out_path = self.tmp / f"out-{self.runs}"
        args: dict[str, Any] = {
            "sandbox": self.sandbox,
            "repo": REPO,
            "base_sha": self.base,
            "branch": "factory/issue-7",
            "message": "Implement #7",
            "author": IDENTITY,
            "git": self.gh.client(),
            "limits": LIMITS,
            **overrides,
        }
        from factory.changeset import publish

        paths = PublishPaths(script_path=SCRIPT, repo_dir=str(self.root), out_path=str(self.out_path))
        return await publish(PublishInput(**args), paths)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    return make_repo(tmp_path)


@pytest.fixture
def harness(repo: tuple[Path, str], tmp_path: Path) -> Harness:
    root, base = repo
    return Harness(root, base, tmp_path, FakeGitHub.from_repo(root, base))


def refusals(result: Published | Refused) -> list[Any]:
    assert isinstance(result, Refused), "expected a refusal"
    return result.refused


BINARY = bytes([0, 1, 2, 0xFF, 0xFE, 13, 10, 13, 0]) + secrets.token_bytes(4000)


def edit_working_tree(root: Path) -> None:
    """The agent's edits: every case a naive archive transfer gets wrong."""
    write(root, "a.txt", "hello\r\nworld\r\n")
    write(root, "crlf.txt", "one\r\ntwo\r\n")
    write(root, "ignored/keep.txt", "kept\n")
    write(root, "app/[slug]/page.tsx", "export default function Page() {}\n")
    (root / "conv").unlink()
    write(root, "conv/inner.txt", "now a directory\n")
    shutil.rmtree(root / "dir2")
    write(root, "dir2", "now a file\n")
    (root / "run.sh").chmod(0o755)
    (root / "gone.txt").unlink()
    write(root, "data.bin", BINARY)
    write(root, "stamp.id", "$Id$\n")
    write(root, "ünï code.txt", "unicode\n")


class TestPublish:
    async def test_publishes_gits_stored_bytes_and_a_tree_equal_to_git_write_tree(self, harness: Harness) -> None:
        root = harness.root
        edit_working_tree(root)
        status_before = git(root, "status", "--porcelain", "-z")
        index_before = sha1((root / ".git/index").read_bytes())

        result = await harness.run()
        assert isinstance(result, Published), result

        # The sandbox's repository is untouched, and only one command ran there, as root.
        assert git(root, "status", "--porcelain", "-z") == status_before
        assert sha1((root / ".git/index").read_bytes()) == index_before
        assert len(harness.sandbox.execs) == 1
        assert harness.sandbox.execs[0][1] is False
        assert harness.sandbox.downloads == [str(harness.out_path)]

        git(root, "add", "-A")
        expected_tree = git(root, "write-tree")
        assert result.tree_sha == expected_tree
        expected: dict[str, Entry] = {}
        for record in git_raw(root, "ls-tree", "-r", "-z", expected_tree).decode().split("\0"):
            if record:
                meta, path = record.split("\t", 1)
                mode, _, sha = meta.split(" ")
                expected[path] = Entry(mode, sha)
        assert harness.gh.files(result.tree_sha) == expected

        for f in result.changed_files:
            if f.sha is not None:
                assert harness.gh.blobs[f.sha] == git_raw(root, "cat-file", "blob", f.sha)

        def blob_at(path: str) -> bytes:
            return harness.gh.blobs[expected[path].sha]

        assert blob_at("a.txt") == b"hello\nworld\n"
        assert blob_at("stamp.id") == b"$Id$\n"
        assert blob_at("data.bin") == BINARY
        assert blob_at("ignored/keep.txt") == b"kept\n"

        assert {f.path: f"{f.status} {f.mode}" for f in result.changed_files} == {
            "a.txt": "M 100644",
            "crlf.txt": "A 100644",
            "ignored/keep.txt": "M 100644",
            "app/[slug]/page.tsx": "A 100644",
            "conv": "D 000000",
            "conv/inner.txt": "A 100644",
            "dir2/x.txt": "D 000000",
            "dir2": "A 100644",
            "run.sh": "M 100755",
            "gone.txt": "D 000000",
            "data.bin": "A 100644",
            "stamp.id": "A 100644",
            "ünï code.txt": "A 100644",
        }

        [tree_call] = [c for c in harness.gh.calls if c[1] == "/trees"]
        entries = tree_call[2]["tree"]
        first_write = next(i for i, e in enumerate(entries) if e["sha"] is not None)
        assert all(e["sha"] is not None for e in entries[first_write:])

        commit = harness.gh.commits[result.commit_sha]
        assert commit["parents"] == [harness.base]
        assert commit["tree"] == result.tree_sha
        assert commit["body"]["message"] == "Implement #7"
        assert commit["body"]["author"] == commit["body"]["committer"] == IDENTITY.to_json()
        assert harness.gh.refs["factory/issue-7"] == result.commit_sha

    async def test_the_github_write_token_never_enters_the_sandbox(self, harness: Harness) -> None:
        write(harness.root, "new.txt", "x\n")
        result = await harness.run()
        assert isinstance(result, Published)
        assert harness.gh.tokens == {TOKEN}
        assert all(TOKEN not in command for command, _ in harness.sandbox.execs)

    async def test_refuses_a_symlink_without_exporting_any_content(self, harness: Harness) -> None:
        write(harness.root, "new.txt", "SECRET-NEW-CONTENT\n")
        (harness.root / "evil").symlink_to("/etc/passwd")
        [refusal] = refusals(await harness.run())
        assert (refusal.kind, refusal.detail, refusal.path) == ("symlink", "evil is a symlink", "evil")
        assert harness.out_path is not None
        assert b"SECRET-NEW-CONTENT" not in harness.out_path.read_bytes()
        assert harness.gh.calls == []

    async def test_refuses_a_file_over_the_size_limit_before_its_content_leaves_the_sandbox(
        self, harness: Harness
    ) -> None:
        write(harness.root, "big.txt", "BIG-CONTENT " * 20 + "\n")
        [refusal] = refusals(await harness.run(limits=PublishLimits(max_changed_files=50, max_file_bytes=100)))
        assert (refusal.kind, refusal.detail, refusal.path, refusal.bytes, refusal.limit) == (
            "file_too_large",
            "big.txt is 241 bytes; the limit is 100",
            "big.txt",
            241,
            100,
        )
        assert harness.out_path is not None
        assert b"BIG-CONTENT" not in harness.out_path.read_bytes()
        assert harness.gh.calls == []

    async def test_refuses_too_many_files(self, harness: Harness) -> None:
        for i in range(3):
            write(harness.root, f"f{i}.txt", f"{i}\n")
        [refusal] = refusals(await harness.run(limits=PublishLimits(max_changed_files=2, max_file_bytes=100)))
        assert refusal.kind == "too_many_files"

    async def test_refuses_changes_under_github_workflows(self, harness: Harness) -> None:
        write(harness.root, ".github/workflows/ci.yml", "on: push\n")
        assert [r.kind for r in refusals(await harness.run())] == ["workflow_file"]

    async def test_refuses_an_empty_change(self, harness: Harness) -> None:
        assert [r.kind for r in refusals(await harness.run())] == ["no_changes"]

    async def test_ignores_agent_written_repo_config_such_as_clean_filters(self, harness: Harness) -> None:
        marker = harness.tmp / "filter-ran"
        git(harness.root, "config", "filter.evil.clean", f"touch {marker}; cat")
        write(harness.root, ".gitattributes", "*.txt filter=evil\n")
        write(harness.root, "new.txt", "x\n")
        assert isinstance(await harness.run(), Published)
        assert not marker.exists()


class TestSubvertedSandbox:
    @staticmethod
    def forged_base(root: Path, base: str) -> tuple[str, str]:
        """Builds a parentless commit whose tree is the base tree plus an `evil` symlink."""
        env = {**GIT_ENV, "GIT_INDEX_FILE": str(root / ".git/forged-index")}
        git_raw(root, "read-tree", base, env=env)
        link = git_raw(root, "hash-object", "-w", "--stdin", env=env, input=b"/etc/passwd").decode().strip()
        git_raw(root, "update-index", "--add", "--cacheinfo", f"120000,{link},evil", env=env)
        tree = git_raw(root, "write-tree", env=env).decode().strip()
        commit = git_raw(root, "commit-tree", tree, "-m", "forged", env=env).decode().strip()
        return commit, tree

    async def test_ignores_git_replace_on_the_base_commit(self, harness: Harness) -> None:
        root = harness.root
        (root / "evil").symlink_to("/etc/passwd")
        write(root, "a.txt", "changed\n")
        commit, tree = self.forged_base(root, harness.base)
        git(root, "replace", "-f", harness.base, commit)
        assert git(root, "rev-parse", f"{harness.base}^{{tree}}") == tree
        assert [r.kind for r in refusals(await harness.run())] == ["symlink"]

    async def test_rejects_a_blob_whose_bytes_dont_match_its_sha(self, harness: Harness) -> None:
        write(harness.root, "new.txt", "original content\n")
        harness.sandbox.tamper = lambda data: data.replace(b"original", b"tampered")
        with pytest.raises(ChangesFileError, match="Blob hash mismatch"):
            await harness.run()
        assert harness.gh.calls == []

    async def test_rejects_a_changes_file_with_trailing_data(self, harness: Harness) -> None:
        write(harness.root, "new.txt", "x\n")
        harness.sandbox.tamper = lambda data: data + b"extra"
        with pytest.raises(RuntimeError, match="Downloaded"):
            await harness.run()
        assert harness.gh.calls == []

    async def test_raises_when_githubs_base_differs_from_the_sandboxs_before_creating_a_ref(
        self, harness: Harness
    ) -> None:
        write(harness.root, "new.txt", "x\n")
        base_tree = harness.gh.commits[harness.base]["tree"]
        harness.gh.files(base_tree)["extra.txt"] = Entry("100644", blob_sha(b"extra\n"))
        with pytest.raises(RuntimeError, match="Tree mismatch"):
            await harness.run()
        assert harness.gh.refs == {}


class TestBranches:
    async def test_refuses_to_move_a_branch_that_points_at_someone_elses_commit(self, harness: Harness) -> None:
        write(harness.root, "new.txt", "x\n")
        foreign = "1" * 40
        harness.gh.commits[foreign] = {"tree": harness.gh.commits[harness.base]["tree"], "parents": [harness.base]}
        harness.gh.refs["factory/issue-7"] = foreign
        with pytest.raises(RuntimeError, match=r"Refusing to move refs/heads/factory/issue-7"):
            await harness.run()
        assert harness.gh.refs["factory/issue-7"] == foreign
        assert not any(method == "PATCH" for method, _, _ in harness.gh.calls)

    async def test_moves_no_branch_once_the_run_no_longer_owns_its_thread(self, harness: Harness) -> None:
        write(harness.root, "new.txt", "x\n")

        async def superseded() -> None:
            raise LookupError("a newer run took over")

        with pytest.raises(LookupError, match="newer run"):
            await harness.run(assert_owned=superseded)
        assert harness.gh.refs == {}

    async def test_is_idempotent_on_retry_with_a_fixed_commit_date(self, harness: Harness) -> None:
        write(harness.root, "new.txt", "x\n")
        first = await harness.run()
        second = await harness.run()
        assert isinstance(first, Published)
        assert isinstance(second, Published)
        assert second.commit_sha == first.commit_sha
        assert len([c for c in harness.gh.calls if c[1] == "/refs"]) == 1

    @pytest.mark.parametrize(
        "branch",
        ["-delete", "a..b", "a//b", "a/", "a.", "a.lock", "a/.hidden", "a b", "a~1", "a^", "a:b", "refs/heads/x*", ""],
    )
    async def test_rejects_unsafe_branch_names_before_touching_the_sandbox(self, harness: Harness, branch: str) -> None:
        assert not is_safe_branch(branch)
        with pytest.raises(ValueError, match="Invalid branch name"):
            await harness.run(branch=branch)
        assert harness.sandbox.execs == []
