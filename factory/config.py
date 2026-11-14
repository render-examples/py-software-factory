"""The active configuration, repository resolution, and run names shared by the gateway and Workflows."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final, Literal

import factory_config
from factory.config_model import FactoryConfig, RepoConfig

# Imported, and so validated, when this module loads: an invalid config stops every service at startup.
_config: FactoryConfig = factory_config.CONFIG


def get_config() -> FactoryConfig:
    """The deployment's config. Tests replace `_config`; nothing else should."""
    return _config


@dataclass(frozen=True)
class RepoContext:
    """One repository a run acts on, resolved from the webhook that started it."""

    owner: str
    name: str
    full_name: str
    """ "owner/name" """
    installation_id: int
    """The GitHub App installation that delivered the webhook; tokens are minted for it."""
    config: RepoConfig


def resolve_repo(full_name: str, installation_id: int) -> RepoContext | None:
    """The configured repository, or None when the factory doesn't act on it."""
    repo_config = get_config().repos.get(full_name)
    if repo_config is None:
        return None
    owner, _, name = full_name.partition("/")
    if not owner or not name:
        return None
    return RepoContext(owner, name, full_name, installation_id, repo_config)


def require_repo(full_name: str, installation_id: int) -> RepoContext:
    """Like resolve_repo, but raises: for tasks, whose input the gateway already checked."""
    repo = resolve_repo(full_name, installation_id)
    if repo is None:
        raise ValueError(f"{full_name} is not configured in factory_config.py")
    return repo


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


TaskName = Literal["triage", "spec-to-pr", "pr-review", "cancel", "refresh-snapshot"]
FlowName = Literal["autopilot"]
RunName = TaskName | FlowName


class Tasks:
    """Task names, shared by the gateway (which starts them) and the Workflows service (which registers them)."""

    TRIAGE: Final = "triage"
    SPEC_TO_PR: Final = "spec-to-pr"
    PR_REVIEW: Final = "pr-review"
    CANCEL: Final = "cancel"
    REFRESH_SNAPSHOT: Final = "refresh-snapshot"


class Flows:
    AUTOPILOT: Final = "autopilot"


FLOW_NAMES: Final[frozenset[str]] = frozenset({Flows.AUTOPILOT})
