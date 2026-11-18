"""Types for `factory_config.py`. Constructing a `FactoryConfig` validates it, so a bad config fails at import."""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from factory.identifiers import is_safe_branch, is_valid_repo_name

Permission = Literal["triage", "write", "maintain", "admin"]

# A lowercase hostname with at least one dot, as the Sandbox egress rules take it; `*.` only as the leftmost label.
_EGRESS_HOST = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RepoConfig(_Config):
    base_branch: str = Field(min_length=1)
    """Branch that specs are implemented against and snapshots are built from."""
    install: str | None = None
    """Installs dependencies in the sandbox. Runs after credentials are removed."""
    verify: tuple[str, ...]
    """Commands that must pass before a PR opens and that PR Reviewer runs."""
    egress: tuple[str, ...] | Literal["allow-all"] = ()
    """HTTPS hosts this repository's sandboxes may reach beyond github.com and the agents' model APIs: package
    registries and anything `install` or `verify` downloads. Exact hostnames; `*.` only as the leftmost label.
    Every other destination is blocked. "allow-all" lifts the restriction for this repository."""


class Labels(_Config):
    triage: str = "factory:triage"
    spec_approved: str = "factory:spec-approved"
    pr_review: str = "factory:pr-review"
    autopilot: str = "factory:autopilot"


class Limits(_Config):
    repair_rounds: int = Field(default=2, ge=0)
    """Fix rounds after a failed verify in Spec-to-PR."""
    max_changed_files: int = Field(default=200, ge=1)
    """Publishing refuses a change with more files than this."""
    max_file_bytes: int = Field(default=1_000_000, ge=1)
    """Publishing refuses any changed file larger than this."""


class SandboxSettings(_Config):
    timeout_seconds: int = Field(default=4 * 60 * 60, ge=60)
    """Render terminates a sandbox after this, even if the task that opened it crashed."""
    brokered_model_access: bool = False
    """Run agents in the sandbox only when its group has a verified provider credential.

    When false, every model loop runs in the Workflow task while its tools still run in the sandbox.
    """


class FactoryConfig(_Config):
    repos: dict[str, RepoConfig]
    """Repositories the factory acts on, keyed by "owner/name". Webhooks from any other repository are ignored."""
    labels: Labels = Labels()
    command_prefix: str = "/factory"
    """Prefix for comment commands, such as `/factory cancel`."""
    minimum_permission: Permission = "write"
    """GitHub permission the triggering actor needs on the repository."""
    limits: Limits = Limits()
    sandbox: SandboxSettings = SandboxSettings()

    @model_validator(mode="after")
    def _check(self) -> Self:
        validate_config(self)
        return self


def assert_egress_hosts(hosts: tuple[str, ...] | list[str], where: str) -> None:
    """Raises naming the first host that isn't a bare hostname: no scheme, port, path, or uppercase."""
    for host in hosts:
        if not _EGRESS_HOST.fullmatch(host):
            raise ValueError(
                f"{where}: egress host {_js(host)} must be a lowercase hostname such as pypi.org or *.example.com"
            )


def validate_config(config: FactoryConfig) -> None:
    """Checks what the field types can't."""
    for full_name, repo in config.repos.items():
        where = f"factory_config.py repos[{_js(full_name)}]"
        if not is_valid_repo_name(full_name):
            raise ValueError(f'{where}: the key must be "owner/name"')
        if not is_safe_branch(repo.base_branch):
            raise ValueError(f"{where}: base_branch {_js(repo.base_branch)} is not a safe Git branch name")
        if repo.egress != "allow-all":
            assert_egress_hosts(repo.egress, where)
    label_values = tuple(config.labels.model_dump().values())
    if len(set(label_values)) != len(label_values):
        raise ValueError("factory_config.py labels must be distinct")
    if not config.command_prefix.strip() or any(c in config.command_prefix for c in "\r\n"):
        raise ValueError("factory_config.py command_prefix must be a non-empty single line")


def _js(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
