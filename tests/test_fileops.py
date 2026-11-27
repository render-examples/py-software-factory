"""sandbox/fileops.py, run the way the sandbox runs it: one JSON request in, one JSON response out."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

FILEOPS = Path(__file__).resolve().parents[1] / "sandbox" / "fileops.py"
needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")


def call(root: Path, op: str, **fields: Any) -> tuple[int, dict[str, Any]]:
    request = json.dumps({"op": op, "root": str(root), **fields})
    result = subprocess.run([sys.executable, "-I", str(FILEOPS), request], capture_output=True, check=False)
    return result.returncode, json.loads(result.stdout.decode())


def ok(root: Path, op: str, **fields: Any) -> dict[str, Any]:
    code, response = call(root, op, **fields)
    assert code == 0, response
    assert "error" not in response, response
    return response


def refused(root: Path, op: str, **fields: Any) -> str:
    code, response = call(root, op, **fields)
    assert code == 0, response
    assert "error" in response, response
    return str(response["error"])


@pytest.fixture
def root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lines.txt").write_text("".join(f"line {n}\n" for n in range(1, 11)))
    return repo


def test_read_returns_a_numbered_window_and_where_to_continue(root: Path) -> None:
    response = ok(root, "read", path="lines.txt", offset=3, limit=2)
    assert response == {
        "path": "lines.txt",
        "text": "     3\tline 3\n     4\tline 4",
        "start": 3,
        "end": 4,
        "total": 10,
    }


def test_read_describes_binary_files_instead_of_returning_them(root: Path) -> None:
    (root / "image.png").write_bytes(b"\x89PNG\x00\x00data")
    assert ok(root, "read", path="image.png") == {"path": "image.png", "binary": True, "size": 10}


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("missing.txt", "missing.txt does not exist"),
        (".", ". is a directory; use list_files"),
        ("../outside.txt", "../outside.txt is outside the repository"),
        ("/etc/passwd", "/etc/passwd is outside the repository"),
    ],
)
def test_read_refuses_what_it_cannot_show(root: Path, path: str, message: str) -> None:
    assert refused(root, "read", path=path) == message


def test_symlinks_are_resolved_before_the_containment_check(root: Path, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("secret")
    (root / "link.txt").symlink_to(tmp_path / "secret.txt")
    (root / "inside.txt").symlink_to(root / "lines.txt")
    assert refused(root, "read", path="link.txt") == "link.txt is outside the repository"
    assert refused(root, "write", path="link.txt", content="x") == "link.txt is outside the repository"
    assert (tmp_path / "secret.txt").read_text() == "secret"
    assert ok(root, "read", path="inside.txt", limit=1)["text"] == "     1\tline 1"


def test_edit_applies_every_replacement_or_none(root: Path) -> None:
    (root / "app.py").write_text("a = 1\nb = 2\n")
    edits = [{"old_text": "a = 1", "new_text": "a = 10"}, {"old_text": "b = 2", "new_text": "b = 20"}]
    assert ok(root, "edit", path="app.py", edits=edits) == {"path": "app.py", "edits": 2}
    assert (root / "app.py").read_text() == "a = 10\nb = 20\n"

    failing = [{"old_text": "a = 10", "new_text": "a = 0"}, {"old_text": "= ", "new_text": "== "}]
    message = refused(root, "edit", path="app.py", edits=failing)
    assert message == "edit 2: old_text occurs 2 times; include more surrounding text in app.py. No edits were applied."
    assert (root / "app.py").read_text() == "a = 10\nb = 20\n"


def test_edit_keeps_the_file_mode_and_line_endings(root: Path) -> None:
    script = root / "run.sh"
    script.write_bytes(b"#!/bin/sh\r\necho one\r\n")
    script.chmod(0o755)
    ok(root, "edit", path="run.sh", edits=[{"old_text": "one", "new_text": "two"}])
    assert script.read_bytes() == b"#!/bin/sh\r\necho two\r\n"
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert sorted(path.name for path in root.iterdir() if path.name.startswith(".fileops-")) == []


def test_write_creates_directories_and_refuses_git_internals(root: Path) -> None:
    assert ok(root, "write", path="pkg/sub/mod.py", content="X = 'é'\n") == {
        "path": "pkg/sub/mod.py",
        "bytes": 9,
        "created": True,
    }
    assert ok(root, "write", path="pkg/sub/mod.py", content="")["created"] is False
    (root / ".git").mkdir()
    assert refused(root, "write", path=".git/hooks/pre-commit", content="x") == "files under .git can't be changed"
    assert refused(root, "edit", path=".git/config", edits=[{"old_text": "a", "new_text": "b"}]) == (
        "files under .git can't be changed"
    )


@needs_rg
def test_list_and_grep_report_paths_relative_to_the_root(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("needle = 1\n")
    (root / "src" / "b.txt").write_text("Needle\n")
    (root / ".gitignore").write_text("ignored.py\n")
    (root / "ignored.py").write_text("needle\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)

    assert ok(root, "list", path="src") == {"path": "src", "files": ["src/a.py", "src/b.txt"], "truncated": False}
    assert ok(root, "list", glob="*.py")["files"] == ["src/a.py"]
    assert ok(root, "grep", pattern="needle")["matches"] == ["src/a.py:1:needle = 1"]
    assert ok(root, "grep", pattern="needle", ignore_case=True, glob="*.txt")["matches"] == ["src/b.txt:1:Needle"]
    assert ok(root, "grep", pattern="absent") == {"path": ".", "matches": [], "truncated": False}
    assert "regex parse error" in refused(root, "grep", pattern="(")


def test_a_staged_request_is_read_once_and_deleted(root: Path, tmp_path: Path) -> None:
    staged = tmp_path / "request.json"
    staged.write_text(json.dumps({"op": "write", "root": str(root), "path": "big.txt", "content": "x" * 200_000}))
    result = subprocess.run(
        [sys.executable, "-I", str(FILEOPS), "--request-file", str(staged)], capture_output=True, check=False
    )
    assert json.loads(result.stdout) == {"path": "big.txt", "bytes": 200_000, "created": True}
    assert not staged.exists()


@pytest.mark.parametrize(
    "request_text",
    ["not json", "[]", json.dumps({"op": "explode", "root": "/"}), json.dumps({"op": "read", "root": "/", "path": 1})],
)
def test_malformed_requests_exit_2(request_text: str) -> None:
    result = subprocess.run([sys.executable, "-I", str(FILEOPS), request_text], capture_output=True, check=False)
    assert result.returncode == 2
    assert "fault" in json.loads(result.stdout)


def test_output_stays_within_the_exec_output_tail(root: Path) -> None:
    (root / "wide.txt").write_text(("é" * 1000 + "\n") * 500)
    response = ok(root, "read", path="wide.txt")
    assert len(json.dumps(response, ensure_ascii=False).encode()) < 256 * 1024
    assert response["end"] < response["total"]


def test_runs_under_the_sandbox_python_version_without_third_party_imports() -> None:
    source = FILEOPS.read_text()
    imports = {line.split()[1].split(".")[0] for line in source.splitlines() if line.startswith(("import ", "from "))}
    assert imports <= {"__future__", "json", "os", "stat", "subprocess", "sys", "tempfile", "collections", "typing"}
    assert os.access(FILEOPS, os.R_OK)
