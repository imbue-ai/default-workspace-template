"""Pin the shape of this workspace's dependency on mngr.

mngr comes from wherever ``pyproject.toml``'s ``[tool.uv.sources]`` entry for
``imbue-mngr`` points, and the tracked tree points it at one commit of the public
mngr repo. The tool environments (``build_workspace.sh``, the update-self refresh)
and the workspace venv (``uv.lock``) all derive from that one entry, so nothing may
drift from it, and no copy of mngr's source may be tracked in the tree.

mngr's packages arrive as built wheels, whose build configs exclude test
infrastructure (``conftest.py``, ``testing.py``, ``*_test.py``), so a module this
tree imports has to actually be in the wheel: ``test_every_imported_mngr_module_is_installed``
holds every ``imbue.*`` import in the tree to that.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PUBLIC_MNGR_REPO = "https://github.com/imbue-ai/mngr"
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

sys.path.insert(0, str(_REPO_ROOT / "system" / "scripts"))

import list_mngr_plugins  # noqa: E402

_MANIFEST = """
[[plugins]]
package = "imbue-mngr-claude"
subdirectory = "libs/mngr_claude"
tools = ["mngr", "system-interface"]

[[plugins]]
package = "imbue-mngr-wait"
subdirectory = "libs/mngr_wait"
tools = ["mngr"]
"""


def _pin() -> dict[str, str]:
    sources = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())["tool"]["uv"][
        "sources"
    ]
    return sources["imbue-mngr"]


def test_mngr_is_pinned_to_a_commit_of_the_public_repo() -> None:
    pin = _pin()
    assert pin["git"] == _PUBLIC_MNGR_REPO, (
        "mngr must come from the public repo, never mngr-internal"
    )
    assert _FULL_SHA.match(pin["rev"]), (
        f"pin a full 40-hex commit, not a branch or tag: {pin['rev']!r}"
    )
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
    off_pin = {
        name: src for name, src in from_mngr.items() if not src.endswith(f"#{rev}")
    }
    assert not off_pin, f"locked at a commit other than the pin: {off_pin}"
    stale_paths = [
        p["name"]
        for p in lock["package"]
        if "system/vendor/mngr" in str(p.get("source", {}))
    ]
    assert not stale_paths, f"still resolved from the vendored tree: {stale_paths}"


def test_every_manifest_plugin_names_a_package_and_its_subdirectory() -> None:
    manifest = tomllib.loads(
        (_REPO_ROOT / "system" / "config" / "mngr_plugins.toml").read_text()
    )
    entries = manifest["plugins"]
    assert entries
    for entry in entries:
        assert entry.get("package", "").startswith("imbue-mngr-"), entry
        assert entry.get("subdirectory", "").startswith("libs/"), entry
        assert entry.get("tools"), entry


def test_no_copy_of_mngr_is_tracked() -> None:
    """The private monorepo copy that used to be committed at system/vendor/mngr must not come back."""
    tracked = subprocess.run(
        ["git", "ls-files", "system/vendor/mngr"],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert tracked == ""


def test_a_git_pin_installs_every_package_from_that_commit() -> None:
    rev = "0123456789abcdef0123456789abcdef01234567"
    pyproject = (
        "[tool.uv.sources]\n"
        f'imbue-mngr = {{ git = "{_PUBLIC_MNGR_REPO}", rev = "{rev}", subdirectory = "libs/mngr" }}\n'
    )
    source = list_mngr_plugins.read_mngr_source(pyproject)

    assert list_mngr_plugins.base_arguments(source) == [
        f"imbue-mngr @ git+{_PUBLIC_MNGR_REPO}@{rev}#subdirectory=libs/mngr"
    ]
    assert list_mngr_plugins.plugin_arguments_for_tool(_MANIFEST, source, "mngr") == [
        "--with",
        f"imbue-mngr-claude @ git+{_PUBLIC_MNGR_REPO}@{rev}#subdirectory=libs/mngr_claude",
        "--with",
        f"imbue-mngr-wait @ git+{_PUBLIC_MNGR_REPO}@{rev}#subdirectory=libs/mngr_wait",
    ]
    assert list_mngr_plugins.plugin_arguments_for_tool(
        _MANIFEST, source, "system-interface"
    ) == [
        "--with",
        f"imbue-mngr-claude @ git+{_PUBLIC_MNGR_REPO}@{rev}#subdirectory=libs/mngr_claude",
    ]


@pytest.mark.parametrize(
    "entry",
    [
        'imbue-mngr = { git = "https://github.com/imbue-ai/mngr", subdirectory = "libs/mngr" }',
        'imbue-mngr = { path = "system/vendor/mngr/libs/mngr", editable = true }',
        'imbue-mngr = { index = "pypi" }',
    ],
)
def test_any_other_source_shape_is_refused(entry: str) -> None:
    with pytest.raises(list_mngr_plugins.MngrPinError):
        list_mngr_plugins.read_mngr_source(f"[tool.uv.sources]\n{entry}\n")


def test_no_source_points_into_the_tree() -> None:
    """Every mngr package comes from the pin; none from a path under system/vendor/mngr."""
    assert "system/vendor/mngr/" not in (_REPO_ROOT / "pyproject.toml").read_text()


_IMPORT = re.compile(r"^\s*(?:from|import)\s+(imbue\.[A-Za-z0-9_.]+)", re.MULTILINE)


def _own_imbue_namespaces() -> set[str]:
    """The ``imbue.<name>`` packages this tree provides itself (``system/**/imbue/<name>/``)."""
    return {
        f"imbue.{path.name}"
        for path in (_REPO_ROOT / "system").glob("*/*/imbue/*")
        if path.is_dir()
    }


def _imported_mngr_modules() -> list[str]:
    """Every ``imbue.*`` module the tree imports that is not one of its own packages."""
    own = _own_imbue_namespaces()
    tracked = subprocess.run(
        ["git", "ls-files", "--", "*.py"],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    modules = {
        match
        for path in tracked
        for match in _IMPORT.findall((_REPO_ROOT / path).read_text(errors="replace"))
        if not any(match == name or match.startswith(f"{name}.") for name in own)
    }
    return sorted(modules)


@pytest.mark.parametrize("module", _imported_mngr_modules())
def test_every_imported_mngr_module_is_installed(module: str) -> None:
    assert importlib.util.find_spec(module) is not None, (
        f"{module} is not in the installed packages"
    )
