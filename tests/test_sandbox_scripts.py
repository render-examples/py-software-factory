from pathlib import Path

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
