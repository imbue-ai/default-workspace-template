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
import use_local_mngr  # noqa: E402

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


def test_no_copy_of_mngr_is_tracked() -> None:
    """A local tree may sit untracked at system/vendor/mngr to be built against;
    the private monorepo copy that used to be committed there must not come back."""
    tracked = subprocess.run(
        ["git", "ls-files", "system/vendor/mngr"], cwd=_REPO_ROOT, check=True, capture_output=True, text=True
    ).stdout
    assert tracked == ""


def test_a_git_pin_installs_every_package_from_that_commit(tmp_path: Path) -> None:
    rev = "0123456789abcdef0123456789abcdef01234567"
    pyproject = (
        "[tool.uv.sources]\n"
        f'imbue-mngr = {{ git = "{_PUBLIC_MNGR_REPO}", rev = "{rev}", subdirectory = "libs/mngr" }}\n'
    )
    source = list_mngr_plugins.read_mngr_source(pyproject, tmp_path)

    assert list_mngr_plugins.base_arguments(source) == [
        f"imbue-mngr @ git+{_PUBLIC_MNGR_REPO}@{rev}#subdirectory=libs/mngr"
    ]
    assert list_mngr_plugins.plugin_arguments_for_tool(_MANIFEST, source, "mngr") == [
        "--with",
        f"imbue-mngr-claude @ git+{_PUBLIC_MNGR_REPO}@{rev}#subdirectory=libs/mngr_claude",
        "--with",
        f"imbue-mngr-wait @ git+{_PUBLIC_MNGR_REPO}@{rev}#subdirectory=libs/mngr_wait",
    ]
    assert list_mngr_plugins.plugin_arguments_for_tool(_MANIFEST, source, "system-interface") == [
        "--with",
        f"imbue-mngr-claude @ git+{_PUBLIC_MNGR_REPO}@{rev}#subdirectory=libs/mngr_claude",
    ]


def test_a_local_tree_installs_every_package_editable_from_it(tmp_path: Path) -> None:
    pyproject = '[tool.uv.sources]\nimbue-mngr = { path = "system/vendor/mngr/libs/mngr", editable = true }\n'
    source = list_mngr_plugins.read_mngr_source(pyproject, tmp_path)
    root = tmp_path / "system" / "vendor" / "mngr"

    assert list_mngr_plugins.base_arguments(source) == ["-e", str(root / "libs" / "mngr")]
    assert list_mngr_plugins.plugin_arguments_for_tool(_MANIFEST, source, "mngr") == [
        "--with-editable",
        str(root / "libs" / "mngr_claude"),
        "--with-editable",
        str(root / "libs" / "mngr_wait"),
    ]


@pytest.mark.parametrize(
    "entry",
    [
        'imbue-mngr = { git = "https://github.com/imbue-ai/mngr", subdirectory = "libs/mngr" }',
        'imbue-mngr = { path = "system/vendor/mngr/libs/mngr" }',
        'imbue-mngr = { path = "somewhere/else", editable = true }',
        'imbue-mngr = { index = "pypi" }',
    ],
)
def test_any_other_source_shape_is_refused(entry: str, tmp_path: Path) -> None:
    with pytest.raises(list_mngr_plugins.MngrPinError):
        list_mngr_plugins.read_mngr_source(f"[tool.uv.sources]\n{entry}\n", tmp_path)


_REV = "0123456789abcdef0123456789abcdef01234567"
_PINNED = f"""[tool.uv.sources]
imbue-mngr = {{ git = "{_PUBLIC_MNGR_REPO}", rev = "{_REV}", subdirectory = "libs/mngr" }}
imbue-mngr-claude = {{ git = "{_PUBLIC_MNGR_REPO}", rev = "{_REV}", subdirectory = "libs/mngr_claude" }}
imbue-common = {{ git = "{_PUBLIC_MNGR_REPO}", rev = "{_REV}", subdirectory = "libs/imbue_common" }}
tk = {{ path = "system/vendor/tk", editable = true }}
"""
_LOCAL = """[tool.uv.sources]
imbue-mngr = { path = "system/vendor/mngr/libs/mngr", editable = true }
imbue-mngr-claude = { path = "system/vendor/mngr/libs/mngr_claude", editable = true }
imbue-common = { path = "system/vendor/mngr/libs/imbue_common", editable = true }
tk = { path = "system/vendor/tk", editable = true }
"""


def test_a_local_tree_rewrites_every_public_repo_pin_to_an_editable_path_into_it() -> None:
    assert use_local_mngr.rewrite_sources(_PINNED) == _LOCAL


def test_rewriting_an_already_local_pyproject_changes_nothing() -> None:
    assert use_local_mngr.rewrite_sources(_LOCAL) == _LOCAL


def test_the_rewritten_sources_are_what_the_installers_read(tmp_path: Path) -> None:
    source = list_mngr_plugins.read_mngr_source(use_local_mngr.rewrite_sources(_PINNED), tmp_path)
    assert source == list_mngr_plugins.LocalTree(root=tmp_path / "system" / "vendor" / "mngr")


def test_without_a_local_tree_the_pin_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_PINNED)

    assert use_local_mngr.use_local_mngr(tmp_path) is False
    assert (tmp_path / "pyproject.toml").read_text() == _PINNED


def test_this_checkout_pins_rather_than_pointing_at_a_local_tree() -> None:
    """The tracked pyproject.toml must never carry the rewritten form."""
    assert "system/vendor/mngr/" not in (_REPO_ROOT / "pyproject.toml").read_text()


_IMPORT = re.compile(r"^\s*(?:from|import)\s+(imbue\.[A-Za-z0-9_.]+)", re.MULTILINE)


def _imported_mngr_modules() -> list[str]:
    """Every ``imbue.*`` module the tree imports that is not one of its own packages."""
    tracked = subprocess.run(
        ["git", "ls-files", "--", "*.py"], cwd=_REPO_ROOT, check=True, capture_output=True, text=True
    ).stdout.split()
    modules = {
        match
        for path in tracked
        for match in _IMPORT.findall((_REPO_ROOT / path).read_text(errors="replace"))
        if not match.startswith("imbue.system_interface")
    }
    return sorted(modules)


@pytest.mark.parametrize("module", _imported_mngr_modules())
def test_every_imported_mngr_module_is_installed(module: str) -> None:
    assert importlib.util.find_spec(module) is not None, f"{module} is not in the installed packages"
