"""Every built-in app manifest validates, names a real supervisord program, is what
that program's registration line passes, and declares a memory band that exists.
"""

import configparser
import re
from pathlib import Path

import pytest
from app_manifest.manifest import MANIFEST_FILENAME, load_manifest
from oom_priority import bands

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APPS_DIR = _REPO_ROOT / "system" / "apps"
_SUPERVISORD_CONF = _REPO_ROOT / "system" / "supervisord.conf"

_MANIFEST_FLAG = re.compile(r"--manifest\s+(\S+)")


def _built_in_manifest_paths() -> list[Path]:
    return sorted(_APPS_DIR.glob(f"*/{MANIFEST_FILENAME}"))


def _command_by_program() -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(_SUPERVISORD_CONF)
    return {
        section.partition(":")[2]: parser[section].get("command", "")
        for section in parser.sections()
        if section.startswith("program:")
    }


def test_every_built_in_app_directory_ships_a_manifest() -> None:
    # The terminal and files apps have no Python yet, but they have manifests;
    # any directory under system/apps/ is an app and must describe itself.
    missing = sorted(
        child.name
        for child in _APPS_DIR.iterdir()
        if child.is_dir() and not (child / MANIFEST_FILENAME).is_file()
    )
    assert missing == [], f"app directories without an {MANIFEST_FILENAME}: {missing}"


@pytest.mark.parametrize("manifest_path", _built_in_manifest_paths(), ids=lambda path: path.parent.name)
def test_built_in_manifest_validates_and_matches_its_program(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    command_by_program = _command_by_program()

    assert manifest.program in command_by_program, (
        f"{manifest_path} names program {manifest.program!r}, which supervisord.conf does not define"
    )
    # The registration line is either in the program's own command or in the
    # launcher script the command runs (the terminal's run_ttyd.sh).
    command = command_by_program[manifest.program]
    registration_sources = [command]
    for token in command.split():
        candidate = _REPO_ROOT / token
        if token.endswith(".sh") and candidate.is_file():
            registration_sources.append(candidate.read_text())
    manifest_flags = [
        match.group(1).strip('"') for source in registration_sources for match in _MANIFEST_FLAG.finditer(source)
    ]
    expected_relative = str(manifest_path.relative_to(_REPO_ROOT))
    assert any(
        flag == expected_relative or (flag.endswith(f"/{MANIFEST_FILENAME}") and manifest_path.parent.name in flag)
        for flag in manifest_flags
    ), f"program {manifest.program!r} does not register with --manifest {expected_relative}: {manifest_flags}"


@pytest.mark.parametrize("manifest_path", _built_in_manifest_paths(), ids=lambda path: path.parent.name)
def test_built_in_manifest_priority_is_a_band(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)

    assert manifest.priority in bands.SERVICE_BANDS, (
        f"{manifest_path} declares priority {manifest.priority!r}, which is not a SERVICE_BANDS key"
    )
    # A built-in never sits in the user band: that would shed it before every
    # user-created app.
    assert manifest.priority != "user"


def test_built_in_manifests_agree_with_the_contract_table() -> None:
    by_name = {manifest.name: manifest for manifest in map(load_manifest, _built_in_manifest_paths())}

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
