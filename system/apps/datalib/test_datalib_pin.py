"""The datalib pieces outside this directory agree with each other: one pinned version, one data root, and no program until first use."""

import configparser
import re
import subprocess
from pathlib import Path
from typing import Final

from datalib_launcher_testing import launcher

# system/apps/datalib/test_datalib_pin.py -> the repository root.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_INSTALL_SCRIPT: Final[Path] = (
    _REPO_ROOT / ".agents/skills/datalib/scripts/install_datalib.sh"
)
_SKILL: Final[Path] = _REPO_ROOT / ".agents/skills/datalib/SKILL.md"
_SYNC_REQUEST: Final[Path] = (
    _REPO_ROOT / ".agents/skills/datalib/scripts/sync_request.py"
)
_PROGRAM_CONF: Final[Path] = _REPO_ROOT / "system/apps/datalib/datalib.conf"
_INSTALLED_PROGRAM: Final[str] = "system/supervisord.conf.d/datalib.conf"

_PINNED_VERSION: Final[re.Pattern[str]] = re.compile(
    r'^readonly PINNED_VERSION="(v[0-9.]+)"$', re.MULTILINE
)
_ANY_VERSION: Final[re.Pattern[str]] = re.compile(r"\bv\d+\.\d+\.\d+\b")
# The skill's shell preamble names the data root under the workspace root.
_SKILL_DATA_ROOT: Final[re.Pattern[str]] = re.compile(
    r'^DATA_ROOT="\$HOME/workspace/([^"]+)"', re.MULTILINE
)
_SYNC_REQUEST_DATA_ROOT: Final[re.Pattern[str]] = re.compile(
    r'^DEFAULT_DATA_ROOT = Path\("([^"]+)"\)$', re.MULTILINE
)


def test_skill_names_only_the_version_the_install_script_installs() -> None:
    """The skill links datalib's agent guide at a tag; a tag other than the installed one describes other binaries."""
    pinned = _PINNED_VERSION.search(_INSTALL_SCRIPT.read_text())
    assert pinned is not None, f"{_INSTALL_SCRIPT} declares no PINNED_VERSION"
    assert set(_ANY_VERSION.findall(_SKILL.read_text())) == {pinned.group(1)}


def test_the_skill_and_its_script_name_the_data_root_the_app_serves() -> None:
    """The skill writes the config, and its script reads the token, in the library the app serves."""
    skill_root = _SKILL_DATA_ROOT.search(_SKILL.read_text())
    assert skill_root is not None, f"{_SKILL} sets no DATA_ROOT under the workspace"
    script_root = _SYNC_REQUEST_DATA_ROOT.search(_SYNC_REQUEST.read_text())
    assert script_root is not None, f"{_SYNC_REQUEST} declares no DEFAULT_DATA_ROOT"
    assert Path(skill_root.group(1)) == launcher.DATA_ROOT
    assert Path(script_root.group(1)) == launcher.DATA_ROOT


def test_nothing_installs_datalib_at_boot() -> None:
    """A unit under env.d runs in every workspace, which would download datalib for users who never ask for it."""
    env_d = _REPO_ROOT / "system/scripts/env.d"
    assert [
        path.name
        for path in env_d.iterdir()
        if "datalib" in path.read_text(errors="replace")
    ] == []


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=_REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout


def test_no_datalib_program_ships_in_supervisord_conf_d() -> None:
    """A program supervisord knows at boot is started by `supervisorctl restart all`, which a backup restore runs.

    Tracked files only: a workspace that has used datalib holds the install script's gitignored copy there.
    """
    tracked = _git("ls-files", "system/supervisord.conf.d").split()
    shipped = [
        path for path in tracked if "program:datalib" in (_REPO_ROOT / path).read_text()
    ]
    assert shipped == []


def test_the_installed_program_is_gitignored() -> None:
    """Otherwise every workspace that used datalib shows the copy as an untracked change."""
    assert _git("check-ignore", _INSTALLED_PROGRAM).strip() == _INSTALLED_PROGRAM


def test_datalib_program_starts_in_the_user_band() -> None:
    """The repository-wide band checks read system/supervisord.conf.d/ only, which no longer holds this program."""
    parser = configparser.RawConfigParser()
    parser.read(_PROGRAM_CONF)
    command = parser.get("program:datalib", "command")
    assert "oom_tag_service.py user " in command
