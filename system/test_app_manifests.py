"""Every built-in app manifest validates, names a real supervisord program, is what
that program's registration line passes, and declares a memory band that exists.
"""

import configparser
import importlib
import re
import tomllib
from pathlib import Path

import pytest
from app_manifest.manifest import MANIFEST_FILENAME, load_manifest
from oom_priority import bands

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APPS_DIR = _REPO_ROOT / "system" / "apps"
_SUPERVISORD_CONF = _REPO_ROOT / "system" / "supervisord.conf"

_MANIFEST_FLAG = re.compile(r"--manifest\s+(\S+)")


# The apps the template ships. Only these are checked: a workspace built from the
# template may carry user-built apps (with a manifest whose priority is ``user``,
# or scaffolded before manifests existed and not yet migrated), and this suite
# runs there too.
_BUILT_IN_APP_PACKAGES = ("browser", "files", "system_interface", "terminal")


def _built_in_manifest_paths() -> list[Path]:
    return [
        _APPS_DIR / package / MANIFEST_FILENAME for package in _BUILT_IN_APP_PACKAGES
    ]


def _command_by_program() -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(_SUPERVISORD_CONF)
    return {
        section.partition(":")[2]: parser[section].get("command", "")
        for section in parser.sections()
        if section.startswith("program:")
    }


def _entry_point_manifest_paths(command: str) -> list[str]:
    """The manifest an app's own entry point registers with, when the program's command ends in one.

    A Python app runs its tool's console script and registers from inside it (the terminal calls
    the sidecar launcher with its manifest), so the manifest path is a constant the script's
    module exports as ``MANIFEST_PATH`` rather than a flag on the command line.
    """
    script_name = command.split()[-1]
    manifest_paths: list[str] = []
    for pyproject_path in _APPS_DIR.glob("*/pyproject.toml"):
        scripts = (
            tomllib.loads(pyproject_path.read_text())
            .get("project", {})
            .get("scripts", {})
        )
        if script_name not in scripts:
            continue
        module_name = scripts[script_name].partition(":")[0]
        manifest_path = getattr(
            importlib.import_module(module_name), "MANIFEST_PATH", None
        )
        if manifest_path is not None:
            manifest_paths.append(str(manifest_path))
    return manifest_paths


def test_every_built_in_app_directory_ships_a_manifest() -> None:
    # Every built-in app describes itself, whatever runs it.
    missing = [
        str(path.relative_to(_REPO_ROOT))
        for path in _built_in_manifest_paths()
        if not path.is_file()
    ]
    assert missing == [], f"built-in apps without an {MANIFEST_FILENAME}: {missing}"


@pytest.mark.parametrize(
    "manifest_path", _built_in_manifest_paths(), ids=lambda path: path.parent.name
)
def test_built_in_manifest_validates_and_matches_its_program(
    manifest_path: Path,
) -> None:
    manifest = load_manifest(manifest_path)
    command_by_program = _command_by_program()

    assert manifest.program in command_by_program, (
        f"{manifest_path} names program {manifest.program!r}, which supervisord.conf does not define"
    )
    # The registration is either a --manifest flag in the program's own command, in
    # a launcher script the command runs, or the MANIFEST_PATH constant of the app
    # entry point the command ends in.
    command = command_by_program[manifest.program]
    registration_sources = [command]
    for token in command.split():
        candidate = _REPO_ROOT / token
        if token.endswith(".sh") and candidate.is_file():
            registration_sources.append(candidate.read_text())
    manifest_flags = [
        match.group(1).strip('"')
        for source in registration_sources
        for match in _MANIFEST_FLAG.finditer(source)
    ] + _entry_point_manifest_paths(command)
    expected_relative = str(manifest_path.relative_to(_REPO_ROOT))
    # A launcher script may pass the manifest as "$REPO_ROOT/<relative path>", so an
    # absolute-looking flag counts when it ends with the repo-relative path.
    assert any(
        flag == expected_relative or flag.endswith(f"/{expected_relative}")
        for flag in manifest_flags
    ), (
        f"program {manifest.program!r} does not register with --manifest {expected_relative}: {manifest_flags}"
    )


@pytest.mark.parametrize(
    "manifest_path", _built_in_manifest_paths(), ids=lambda path: path.parent.name
)
def test_built_in_manifest_priority_is_a_band(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)

    assert manifest.priority in bands.SERVICE_BANDS, (
        f"{manifest_path} declares priority {manifest.priority!r}, which is not a SERVICE_BANDS key"
    )
    # A built-in never sits in the user band: that would shed it before every
    # user-created app.
    assert manifest.priority != "user"


def test_built_in_manifests_agree_with_the_contract_table() -> None:
    by_name = {
        manifest.name: manifest
        for manifest in map(load_manifest, _built_in_manifest_paths())
    }

    assert by_name["system_interface"].internal is True
    assert by_name["system_interface"].critical is True
    assert by_name["terminal"].critical is True
    assert by_name["files"].critical is False
    assert by_name["browser"].critical is False
    for name in ("terminal", "files", "browser"):
        assert by_name[name].instances is True
        assert by_name[name].default_shortcut is not None
        assert by_name[name].default_shortcut.action == "new"
        assert [action.id for action in by_name[name].actions] == ["new"]
    assert by_name["terminal"].instances_url == "http://127.0.0.1:7682"
    assert by_name["files"].instances_url == "http://127.0.0.1:8301"
    assert by_name["browser"].instances_url is None
