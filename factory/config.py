from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final, Literal

import factory_config
from factory.config_model import FactoryConfig, RepoConfig

_config: FactoryConfig = factory_config.CONFIG


def get_config() -> FactoryConfig:
    return _config


@dataclass(frozen=True)
class RepoContext:
    owner: str
    name: str
    full_name: str
    installation_id: int
    config: RepoConfig


def resolve_repo(full_name: str, installation_id: int) -> RepoContext | None:
    repo_config = get_config().repos.get(full_name)
    if repo_config is None:
        return None
    owner, _, name = full_name.partition("/")
    if not owner or not name:
        return None
    return RepoContext(owner, name, full_name, installation_id, repo_config)


def require_repo(full_name: str, installation_id: int) -> RepoContext:
    repo = resolve_repo(full_name, installation_id)
    if repo is None:
        raise ValueError(f"{full_name} is not configured in factory_config.py")
    return repo


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


TaskName = Literal["triage", "spec-to-pr", "pr-review", "autopilot", "cancel", "refresh-snapshot"]
RunName = TaskName


class Tasks:
    TRIAGE: Final = "triage"
    SPEC_TO_PR: Final = "spec-to-pr"
    PR_REVIEW: Final = "pr-review"
    AUTOPILOT: Final = "autopilot"
    CANCEL: Final = "cancel"
    REFRESH_SNAPSHOT: Final = "refresh-snapshot"
