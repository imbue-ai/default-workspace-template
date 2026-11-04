"""Telling vendored code apart from the template's own, for the root checks that scan the tree."""

from __future__ import annotations

from pathlib import Path


def resolves_into_vendor(path: Path, repo_root: Path) -> bool:
    """Whether ``path`` is under ``system/vendor/`` once links are followed: content the template carries but does not own."""
    return (repo_root / "system" / "vendor").resolve() in path.resolve().parents
