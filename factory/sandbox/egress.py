from __future__ import annotations

from collections.abc import Iterable

from render.experimental.sandbox.types import EgressRule, NetworkPolicy

from factory.config import RepoContext
from factory.config_model import assert_egress_hosts

GITHUB_HOST = "github.com"
DEBIAN_HOST = "deb.debian.org"
PYPI_HOSTS = ("pypi.org", "files.pythonhosted.org")
GITHUB_RELEASE_ASSET_HOST = "release-assets.githubusercontent.com"


def task_egress(repo: RepoContext) -> NetworkPolicy:
    return _policy(repo, (GITHUB_HOST,))


def builder_egress(repo: RepoContext) -> NetworkPolicy:
    return _policy(
        repo,
        (
            GITHUB_HOST,
            GITHUB_RELEASE_ASSET_HOST,
            DEBIAN_HOST,
            *PYPI_HOSTS,
        ),
    )


def egress_hint(repo: RepoContext, policy: NetworkPolicy) -> str:
    if policy.type != "allow-list":
        return ""
    hosts = ", ".join(rule.domain for rule in policy.rules)
    return (
        f"\nSandbox egress is limited to {hosts}. If install needs another host, add it to egress for "
        f'"{repo.full_name}" in factory_config.py.'
    )


def _policy(repo: RepoContext, base_hosts: Iterable[str]) -> NetworkPolicy:
    if repo.config.egress == "allow-all":
        return NetworkPolicy(type="allow-all")
    hosts = list(dict.fromkeys((*base_hosts, *repo.config.egress)))
    assert_egress_hosts(hosts, f"sandbox egress for {repo.full_name}")
    return NetworkPolicy(type="allow-list", rules=[EgressRule(domain=host) for host in hosts])
