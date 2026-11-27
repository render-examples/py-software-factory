from __future__ import annotations

from typing import Protocol

from factory.config import get_config
from factory.github import PERMISSIONS, Permission


class PermissionGitHub(Protocol):
    async def get_permission(self, login: str) -> Permission: ...


async def check_permission(gh: PermissionGitHub, actor: str) -> bool:
    have = await gh.get_permission(actor)
    return PERMISSIONS.index(have) >= PERMISSIONS.index(get_config().minimum_permission)
