import getpass
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from factory.sandbox.snapshot import _env_file

ROOT = Path(__file__).resolve().parents[1]


def script(name: str) -> str:
    return (ROOT / "sandbox" / name).read_text()


def test_run_agent_is_protocol_v3_without_key_material() -> None:
    source = script("run-agent.sh")
    assert "PYTHONPATH=" in source
    assert "-m factory.runner" in source
    assert "--key-stdin" not in source
    assert "run_dir/key" not in source
    assert "FACTORY_RUNNER_NODE" not in source


def test_verify_uses_python_and_has_no_node_or_dumpability_probe() -> None:
    source = script("verify.sh")
    assert "FACTORY_PYTHON" in source
    assert '"protocol": 3' in source
    assert "FACTORY_NODE" not in source
    assert "non_dumpable" not in source
    assert "/proc/" not in source


def test_verify_executes_with_exported_generated_environment(tmp_path: Path) -> None:
    app = tmp_path / "app"
    repo = tmp_path / "repo"
    scratch = tmp_path / "scratch"
    (app / "build").mkdir(parents=True)
    (scratch / "home").mkdir(parents=True)
    (scratch / "tmp").mkdir()

    for relative in (
        "verify.sh",
        "as-runner.sh",
        "run-agent.sh",
        "detach.sh",
        "lockfile-hash.sh",
        "build/git-credential-staged.sh",
        "build/scrub-credentials.sh",
    ):
        destination = app / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "sandbox" / relative, destination)

    runner = b"runner archive"
    (app / "runner.zip").write_bytes(runner)
    (app / "runner.sha256").write_text(hashlib.sha256(runner).hexdigest() + "\n")
    (app / "gitconfig").write_text("")

    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Factory Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "factory@example.com"], check=True)
    (repo / "tracked.txt").write_text("tracked\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "fixture"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/acme/example.git"],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (app / "manifest.json").write_text(json.dumps({"sha": head}))

    generated = _env_file().decode().splitlines()
    assert all(line.startswith("export ") for line in generated)
    tools = tmp_path / "bin"
    tools.mkdir()
    for tool in ("rg", "timeout"):
        (tools / tool).write_text('#!/bin/sh\nexec "$@"\n')
        (tools / tool).chmod(0o755)
    values = {
        "FACTORY_ROOT": str(tmp_path),
        "FACTORY_APP_DIR": str(app),
        "FACTORY_REPO_DIR": str(repo),
        "FACTORY_SCRATCH_DIR": str(scratch),
        "FACTORY_RUN_AS": getpass.getuser(),
        "FACTORY_PYTHON": sys.executable,
        "FACTORY_PATH": f"{tools}:{os.environ['PATH']}",
    }
    (app / "env").write_text("".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items()))

    result = subprocess.run(
        ["bash", str(app / "verify.sh")],
        env={**os.environ, "HOME": str(scratch / "home")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.splitlines()[-1])
    assert report["runAs"] == getpass.getuser()
    assert report["protocol"] == 3


def test_install_tools_uses_pinned_uv_managed_python_312_and_venv() -> None:
    source = script("install-tools.sh")
    assert "UV_VERSION=" in source
    assert "PYTHON_VERSION=3.12." in source
    assert "UV_PYTHON_INSTALL_DIR=" in source
    assert 'uv" venv' in source
    assert '"$root/venv"' in source
    assert "--require-hashes" in source
    assert "runner-requirements.txt" in source
    assert (ROOT / "sandbox" / "runner-requirements.txt").is_file()
    assert "node" not in source.lower()


def test_detach_validates_paths_and_writes_exit_atomically() -> None:
    source = script("detach.sh")
    assert "/tmp/factory-detached-" in source
    assert "unsafe run directory" in source
    assert 'timeout --kill-after=10s "$seconds" "$@"' in source
    assert 'mv "$run_dir/exit.next" "$run_dir/exit"' in source
