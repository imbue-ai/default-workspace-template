"""Pin the shape of this workspace's dependency on mngr.

mngr is installed from the public mngr repo at one commit, and that commit lives in
exactly one place: ``pyproject.toml``'s ``[tool.uv.sources]`` entry for ``imbue-mngr``.
The tool environments (``build_workspace.sh``, the update-self refresh) and the
workspace venv (``uv.lock``) all derive from it, so nothing may drift from it, and no
copy of mngr's source may reappear in the tree.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PUBLIC_MNGR_REPO = "https://github.com/imbue-ai/mngr"
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _pin() -> dict[str, str]:
    sources = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())["tool"]["uv"]["sources"]
    return sources["imbue-mngr"]


def test_mngr_is_pinned_to_a_commit_of_the_public_repo() -> None:
    pin = _pin()
    assert pin["git"] == _PUBLIC_MNGR_REPO, "mngr must come from the public repo, never mngr-internal"
    assert _FULL_SHA.match(pin["rev"]), f"pin a full 40-hex commit, not a branch or tag: {pin['rev']!r}"
    assert pin["subdirectory"] == "libs/mngr"


def test_every_locked_mngr_package_is_at_the_pinned_commit() -> None:
    """One resolution, one commit: uv rejects two revs of a repo, and the tools must match the venv."""
    rev = _pin()["rev"]
    lock = tomllib.loads((_REPO_ROOT / "uv.lock").read_text())
    from_mngr = {
        package["name"]: package["source"]["git"]
        for package in lock["package"]
        if _PUBLIC_MNGR_REPO in str(package.get("source", {}).get("git", ""))
    }
    assert "imbue-mngr" in from_mngr
    off_pin = {name: src for name, src in from_mngr.items() if not src.endswith(f"#{rev}")}
    assert not off_pin, f"locked at a commit other than the pin: {off_pin}"
    stale_paths = [p["name"] for p in lock["package"] if "system/vendor/mngr" in str(p.get("source", {}))]
    assert not stale_paths, f"still resolved from the vendored tree: {stale_paths}"


def test_every_manifest_plugin_names_a_package_and_its_subdirectory() -> None:
    manifest = tomllib.loads((_REPO_ROOT / "system" / "config" / "mngr_plugins.toml").read_text())
    entries = manifest["plugins"]
    assert entries
    for entry in entries:
        assert entry.get("package", "").startswith("imbue-mngr-"), entry
        assert entry.get("subdirectory", "").startswith("libs/"), entry
        assert entry.get("tools"), entry


def test_no_vendored_copy_of_mngr_exists() -> None:
    """The tree that used to hold a full copy of the private monorepo must not come back."""
    assert not (_REPO_ROOT / "system" / "vendor" / "mngr").exists()
