from __future__ import annotations

from dataclasses import dataclass

from factory.config_model import FactoryConfig, Labels, Limits, RepoConfig, SandboxSettings

REPO = "acme/widget"
BUILD_COMMAND = "npm run build"
TEST_COMMAND = "npm test"


@dataclass(frozen=True)
class RepositoryCase:
    repo: str
    base_branch: str
    verify: tuple[str, ...]


REPOSITORY_CASES = (
    RepositoryCase("acme/docs", "docs", ()),
    RepositoryCase("acme/api", "develop", ("npm run check",)),
    RepositoryCase("other/worker", "release/candidate", ("npm run lint", "npm run typecheck", "npm test")),
)

TEST_CONFIG = FactoryConfig(
    repos={
        REPO: RepoConfig(base_branch="main", install="npm ci", verify=(BUILD_COMMAND, TEST_COMMAND)),
        **{c.repo: RepoConfig(base_branch=c.base_branch, verify=c.verify) for c in REPOSITORY_CASES},
    },
    labels=Labels(
        triage="factory:triage",
        spec_approved="factory:spec-approved",
        pr_review="factory:pr-review",
        autopilot="factory:autopilot",
    ),
    command_prefix="/factory",
    minimum_permission="write",
    limits=Limits(repair_rounds=2, max_changed_files=200, max_file_bytes=1_000_000),
    sandbox=SandboxSettings(timeout_seconds=4 * 60 * 60),
)
