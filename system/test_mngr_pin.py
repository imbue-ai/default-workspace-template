"""Pin the shape of this workspace's dependency on mngr.

mngr comes from wherever ``pyproject.toml``'s ``[tool.uv.sources]`` entry for
``imbue-mngr`` points, and the tracked tree points it at one commit of the public
mngr repo, or -- on a branch iterating on a paired mngr change -- of the private
mngr-internal repo. The tool environments (``build_workspace.sh``, the update-self
refresh) and the workspace venv (``uv.lock``) all derive from that one entry, so
nothing may drift from it, and no copy of mngr's source may be tracked in the tree.
A private pin never ships: the release gates on the mngr side refuse it.

mngr's packages arrive as built wheels, whose build configs exclude test
infrastructure (``conftest.py``, ``testing.py``, ``*_test.py``), so a module this
tree imports has to actually be in the wheel: ``test_every_imported_mngr_module_is_installed``
holds every ``imbue.*`` import in the tree to that.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PUBLIC_MNGR_REPO = "https://github.com/imbue-ai/mngr"
_INTERNAL_MNGR_REPO = "https://github.com/imbue-ai/mngr-internal"
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

sys.path.insert(0, str(_REPO_ROOT / "system" / "scripts"))

import list_mngr_plugins  # noqa: E402

_MNGR_REPOS = (_PUBLIC_MNGR_REPO, _INTERNAL_MNGR_REPO)

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


def test_mngr_is_pinned_to_a_commit_of_a_mngr_repo() -> None:
    pin = _pin()
    assert pin["git"] in _MNGR_REPOS, (
        "mngr must come from the public mirror or from mngr-internal, nowhere else"
    )
    assert _FULL_SHA.match(pin["rev"]), (
        f"pin a full 40-hex commit, not a branch or tag: {pin['rev']!r}"
    )
    assert pin["subdirectory"] == "libs/mngr"


def test_every_locked_mngr_package_is_at_the_pinned_commit() -> None:
    """One resolution, one repo, one commit: the tools and the venv resolve from the same lock."""
    pin = _pin()
    rev = pin["rev"]
    lock = tomllib.loads((_REPO_ROOT / "uv.lock").read_text())
    from_mngr = {
        package["name"]: package["source"]["git"]
        for package in lock["package"]
        if any(
            repo in str(package.get("source", {}).get("git", ""))
            for repo in _MNGR_REPOS
        )
    }
    assert "imbue-mngr" in from_mngr
    off_pin = {
        name: src
        for name, src in from_mngr.items()
        if not (src.startswith(pin["git"]) and src.endswith(f"#{rev}"))
    }
    assert not off_pin, f"locked at a repo or commit other than the pin: {off_pin}"
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
    """No copy of mngr's source is tracked under system/vendor/mngr."""
    tracked = subprocess.run(
        ["git", "ls-files", "system/vendor/mngr"],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert tracked == ""


@pytest.mark.parametrize("repo", _MNGR_REPOS)
def test_a_git_pin_installs_every_package_from_that_commit(repo: str) -> None:
    rev = "0123456789abcdef0123456789abcdef01234567"
    pyproject = (
        "[tool.uv.sources]\n"
        f'imbue-mngr = {{ git = "{repo}", rev = "{rev}", subdirectory = "libs/mngr" }}\n'
    )
    source = list_mngr_plugins.read_mngr_source(pyproject)

    assert list_mngr_plugins.base_arguments(source) == [
        f"imbue-mngr @ git+{repo}@{rev}#subdirectory=libs/mngr"
    ]
    assert list_mngr_plugins.plugin_arguments_for_tool(_MANIFEST, source, "mngr") == [
        "--with",
        f"imbue-mngr-claude @ git+{repo}@{rev}#subdirectory=libs/mngr_claude",
        "--with",
        f"imbue-mngr-wait @ git+{repo}@{rev}#subdirectory=libs/mngr_wait",
    ]
    assert list_mngr_plugins.plugin_arguments_for_tool(
        _MANIFEST, source, "system-interface"
    ) == [
        "--with",
        f"imbue-mngr-claude @ git+{repo}@{rev}#subdirectory=libs/mngr_claude",
    ]


def test_the_pin_kind_names_the_repo() -> None:
    rev = "0123456789abcdef0123456789abcdef01234567"
    public = list_mngr_plugins.read_mngr_source(
        f'[tool.uv.sources]\nimbue-mngr = {{ git = "{_PUBLIC_MNGR_REPO}", rev = "{rev}" }}\n'
    )
    internal = list_mngr_plugins.read_mngr_source(
        f'[tool.uv.sources]\nimbue-mngr = {{ git = "{_INTERNAL_MNGR_REPO}", rev = "{rev}" }}\n'
    )
    assert (public.is_internal, public.kind) == (False, "public")
    assert (internal.is_internal, internal.kind) == (True, "internal")


@pytest.mark.parametrize(
    "entry",
    [
        'imbue-mngr = { git = "https://github.com/imbue-ai/mngr", subdirectory = "libs/mngr" }',
        'imbue-mngr = { git = "https://github.com/imbue-ai/mngr-internal", subdirectory = "libs/mngr" }',
        'imbue-mngr = { git = "https://github.com/someone-else/mngr", rev = "0123456789abcdef0123456789abcdef01234567" }',
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


def _own_imbue_namespaces() -> set[str]:
    """The ``imbue.<name>`` packages this tree provides itself (``system/**/imbue/<name>/``)."""
    return {
        f"imbue.{path.name}"
        for path in (_REPO_ROOT / "system").glob("*/*/imbue/*")
        if path.is_dir()
    }


def _imported_names(source: str) -> set[str]:
    """Every dotted ``imbue.*`` name an import statement in ``source`` names.

    ``from imbue.mngr.utils import testing`` names ``imbue.mngr.utils.testing`` as well as
    its package: a name imported from a package may be a module the wheel excludes, and
    that is exactly what this file holds the tree to. A name that turns out to be an
    attribute rather than a module has no spec and is dropped by :func:`_module_names`.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return {name for name in names if name == "imbue" or name.startswith("imbue.")}


def _module_names(names: set[str], own: set[str]) -> list[str]:
    """``names`` that are modules this tree does not provide itself.

    A name that no finder resolves at all is a module the installed packages lack, which is
    the failure this file reports; one that resolves to an attribute of its package (no
    spec, but the package has it) is not a module and is dropped.
    """
    resolved: set[str] = set()
    for name in names:
        if any(name == package or name.startswith(f"{package}.") for package in own):
            continue
        parent, _, attribute = name.rpartition(".")
        if parent and attribute and _is_attribute_of(parent, attribute):
            continue
        resolved.add(name)
    return sorted(resolved)


def _is_attribute_of(parent: str, attribute: str) -> bool:
    """Whether ``attribute`` is a non-module member of the importable package ``parent``."""
    try:
        module = importlib.import_module(parent)
    except Exception:
        return False
    member = getattr(module, attribute, None)
    return member is not None and not isinstance(member, ModuleType)


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
    names: set[str] = set()
    for path in tracked:
        names |= _imported_names((_REPO_ROOT / path).read_text(errors="replace"))
    return _module_names(names, own)


@pytest.mark.parametrize("module", _imported_mngr_modules())
def test_every_imported_mngr_module_is_installed(module: str) -> None:
    assert importlib.util.find_spec(module) is not None, (
        f"{module} is not in the installed packages"
    )


import set_mngr_pin  # noqa: E402

_OTHER_REV = "fedcba9876543210fedcba9876543210fedcba98"
_MAINTAINED_FILES = (
    "pyproject.toml",
    set_mngr_pin.SETTINGS_PATH,
    set_mngr_pin.DOCKERFILE_PATH,
)


def _copy_maintained_files(root: Path) -> None:
    for path in _MAINTAINED_FILES:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text((_REPO_ROOT / path).read_text())


def _texts(root: Path) -> dict[str, str]:
    return {path: (root / path).read_text() for path in _MAINTAINED_FILES}


def test_the_tracked_tree_agrees_with_its_pin() -> None:
    """The pin's repo and the BuildKit lines set_mngr_pin.py maintains always move together."""
    assert (
        set_mngr_pin.check_tree(_REPO_ROOT)
        == list_mngr_plugins.read_mngr_source(
            (_REPO_ROOT / "pyproject.toml").read_text()
        ).kind
    )


def test_a_pin_move_to_the_other_repo_and_back_restores_the_tree(
    tmp_path: Path,
) -> None:
    _copy_maintained_files(tmp_path)
    original = _texts(tmp_path)
    original_kind = set_mngr_pin.check_tree(tmp_path)
    original_rev = _pin()["rev"]
    other_kind = "public" if original_kind == "internal" else "internal"

    written = set_mngr_pin.set_pin(tmp_path, other_kind, _OTHER_REV)

    assert sorted(written) == sorted(_MAINTAINED_FILES), (
        "every maintained file changes with the repo"
    )
    assert set_mngr_pin.check_tree(tmp_path) == other_kind
    moved = _texts(tmp_path)
    assert all(moved[path] != original[path] for path in _MAINTAINED_FILES)
    for lines in set_mngr_pin.BUILDKIT_LINES:
        public, internal = lines.count(moved[lines.path])
        assert (public, internal) == (
            (0, internal) if other_kind == "internal" else (public, 0)
        )
        assert public + internal >= 1

    assert set_mngr_pin.set_pin(tmp_path, original_kind, original_rev) == written
    assert _texts(tmp_path) == original


def test_a_pin_move_within_the_same_repo_touches_only_the_pyproject(
    tmp_path: Path,
) -> None:
    _copy_maintained_files(tmp_path)
    original = _texts(tmp_path)
    kind = set_mngr_pin.check_tree(tmp_path)

    assert set_mngr_pin.set_pin(tmp_path, kind, _OTHER_REV) == ["pyproject.toml"]

    moved = _texts(tmp_path)
    assert moved[set_mngr_pin.SETTINGS_PATH] == original[set_mngr_pin.SETTINGS_PATH]
    assert moved[set_mngr_pin.DOCKERFILE_PATH] == original[set_mngr_pin.DOCKERFILE_PATH]
    assert set_mngr_pin.check_tree(tmp_path) == kind
    lock_sources = tomllib.loads(moved["pyproject.toml"])["tool"]["uv"]["sources"]
    assert {
        source["rev"]
        for source in lock_sources.values()
        if isinstance(source, dict) and source.get("git") in _MNGR_REPOS
    } == {_OTHER_REV}


def test_a_tree_whose_buildkit_lines_disagree_with_the_pin_fails_the_check_until_repinned(
    tmp_path: Path,
) -> None:
    _copy_maintained_files(tmp_path)
    kind = set_mngr_pin.check_tree(tmp_path)
    other_kind = "public" if kind == "internal" else "internal"
    pyproject = tmp_path / "pyproject.toml"
    other_repo = _INTERNAL_MNGR_REPO if other_kind == "internal" else _PUBLIC_MNGR_REPO
    pyproject.write_text(
        set_mngr_pin.rewrite_pin_sources(pyproject.read_text(), other_repo, _OTHER_REV)
    )

    with pytest.raises(set_mngr_pin.MngrPinTreeError, match=f"shaped for a {kind} pin"):
        set_mngr_pin.check_tree(tmp_path)

    set_mngr_pin.set_pin(tmp_path, other_kind, _OTHER_REV)

    assert set_mngr_pin.check_tree(tmp_path) == other_kind


def test_a_file_mixing_both_shapes_is_reported_by_name() -> None:
    settings = set_mngr_pin.BUILDKIT_LINES[0]
    dockerfile_lines = set_mngr_pin.BUILDKIT_LINES[1:]
    files = {
        settings.path: f"a = [{settings.public}]\nb = [{settings.internal}]\n",
        **{lines.path: f"{lines.public}\n" for lines in dockerfile_lines},
    }

    with pytest.raises(
        set_mngr_pin.MngrPinTreeError,
        match=f"{settings.path} mixes 1 public and 1 private",
    ):
        set_mngr_pin.buildkit_lines_kind(files)


def test_a_file_with_none_of_the_maintained_lines_is_reported_by_name() -> None:
    files = {lines.path: "" for lines in set_mngr_pin.BUILDKIT_LINES}

    with pytest.raises(
        set_mngr_pin.MngrPinTreeError, match=set_mngr_pin.DOCKERFILE_PATH
    ):
        set_mngr_pin.buildkit_lines_kind(files)


def test_rewrite_pin_sources_moves_every_mngr_entry_and_nothing_else() -> None:
    old = "0123456789abcdef0123456789abcdef01234567"
    text = (
        "[tool.uv.sources]\n"
        f'imbue-mngr = {{ git = "{_PUBLIC_MNGR_REPO}", rev = "{old}", subdirectory = "libs/mngr" }}\n'
        f'imbue-mngr-claude = {{ git = "{_PUBLIC_MNGR_REPO}", rev = "{old}", subdirectory = "libs/mngr_claude" }}\n'
        'tk = { path = "system/vendor/tk", editable = true }\n'
    )

    assert set_mngr_pin.rewrite_pin_sources(text, _INTERNAL_MNGR_REPO, _OTHER_REV) == (
        "[tool.uv.sources]\n"
        f'imbue-mngr = {{ git = "{_INTERNAL_MNGR_REPO}", rev = "{_OTHER_REV}", subdirectory = "libs/mngr" }}\n'
        f'imbue-mngr-claude = {{ git = "{_INTERNAL_MNGR_REPO}", rev = "{_OTHER_REV}", subdirectory = "libs/mngr_claude" }}\n'
        'tk = { path = "system/vendor/tk", editable = true }\n'
    )


def test_the_cli_reports_a_private_pin_as_not_public(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _copy_maintained_files(tmp_path)
    set_mngr_pin.set_pin(tmp_path, "internal", _OTHER_REV)

    assert set_mngr_pin.main(["--check", "--repo-root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "internal"
    assert set_mngr_pin.main(["--require-public", "--repo-root", str(tmp_path)]) == 1
    assert "never merges to main" in capsys.readouterr().err

    set_mngr_pin.set_pin(tmp_path, "public", _OTHER_REV)

    assert set_mngr_pin.main(["--require-public", "--repo-root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "public"
