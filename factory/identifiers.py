from __future__ import annotations

import re

_BRANCH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
_REPOSITORY_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")


def is_safe_branch(branch: str) -> bool:
    return (
        len(branch) <= 200
        and _BRANCH.fullmatch(branch) is not None
        and ".." not in branch
        and "//" not in branch
        and not branch.endswith(("/", ".", ".lock"))
        and not any(segment.startswith(".") for segment in branch.split("/"))
    )


def is_valid_repo_name(repository: str) -> bool:
    segments = repository.split("/")
    return (
        len(segments) == 2
        and all(segment not in {"", ".", ".."} for segment in segments)
        and all(_REPOSITORY_SEGMENT.fullmatch(segment) is not None for segment in segments)
    )
