"""Shared, side-effect-free helpers for setup commands."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from factory.identifiers import is_valid_repo_name

ROOT = Path(__file__).resolve().parents[1]
SANDBOX_GROUP_RE = re.compile(r"^sbg-[A-Za-z0-9]+$")


@dataclass(frozen=True)
class Check:
    """One preflight or doctor result."""

    name: str
    ok: bool
    detail: str
    required: bool = True


def parse_env_file(text: str) -> dict[str, str]:
    """Parse the conservative KEY=VALUE subset accepted by Render's bulk importer."""
    values: dict[str, str] = {}
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"line {line_number}: invalid environment variable name {key!r}")
        value = value.strip()
        if value[:1] in {"'", '"'}:
            try:
                parts = shlex.split(value, posix=True)
            except ValueError as error:
                raise ValueError(f"line {line_number}: {error}") from error
            if len(parts) != 1:
                raise ValueError(f"line {line_number}: quoted value must be one token")
            value = parts[0]
        values[key] = value
    return values


def load_environment(path: Path | None, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Merge an optional env file under the real process environment."""
    merged = parse_env_file(path.read_text()) if path and path.exists() else {}
    merged.update(environ or os.environ)
    return merged


def require_values(environment: Mapping[str, str], names: Iterable[str]) -> list[Check]:
    return [
        Check(name, bool(environment.get(name, "").strip()), "set" if environment.get(name, "").strip() else "missing")
        for name in names
    ]


def validate_repository(value: str) -> str:
    value = value.strip()
    if not is_valid_repo_name(value):
        raise ValueError("repository must be OWNER/NAME")
    return value


def normalize_gateway_url(value: str) -> str:
    """Return a public gateway base URL with no query, fragment, or trailing slash."""
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("gateway URL must be an absolute http(s) URL")
    if parsed.query or parsed.fragment:
        raise ValueError("gateway URL must not include a query or fragment")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("a non-local gateway URL must use HTTPS")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def command_check(command: Sequence[str], *, required: bool = True) -> Check:
    """Run a read-only version command without inheriting secret-bearing stdin."""
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return Check(command[0], False, str(error), required)
    output = (result.stdout or result.stderr).strip().splitlines()
    detail = output[0] if output else f"exit {result.returncode}"
    return Check(command[0], result.returncode == 0, detail, required)


def print_checks(checks: Sequence[Check], *, json_output: bool = False) -> int:
    if json_output:
        print(json.dumps([asdict(check) for check in checks], indent=2))
    else:
        for check in checks:
            marker = "PASS" if check.ok else ("FAIL" if check.required else "WARN")
            print(f"{marker:4} {check.name}: {check.detail}")
    return 1 if any(check.required and not check.ok for check in checks) else 0


def write_private(path: Path, content: str) -> None:
    """Write setup output with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(content)


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2
