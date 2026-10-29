"""The sandbox's supervisord config and boot script, as files: what they name has to exist in the tree."""

import configparser
import re
import subprocess
from pathlib import Path

from auth_sandbox.image_build import BOOT_SCRIPT_SOURCE, SUPERVISORD_CONF_SOURCE
from auth_sandbox.primitives import (
    HOST_ID_ENV_VAR,
    INSTALL_DIR,
    RUNTIME_DIR,
    SCREEN_SERVICE_URL,
    WORKSPACE_ROOT,
)

_REPO_ROOT = Path(__file__).resolve().parents[5]
_SECTION_PATTERN = re.compile(
    r"^\[(program|eventlistener|group):([^\]]+)\]", re.MULTILINE
)
_SANDBOX_PROGRAMS = frozenset(
    {
        "screen",
        "screen-xorg",
        "screen-pulse",
        "screen-session",
        "screen-neko",
        "screen-watch",
        "share-gateway",
        "owner-exec",
    }
)


def _included_dropins() -> list[Path]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(_REPO_ROOT / SUPERVISORD_CONF_SOURCE)
    assert parser.get("supervisord", "nodaemon") == "true"
    included: list[Path] = []
    for pattern in parser.get("include", "files").split():
        path = Path(pattern)
        assert (
            path.is_absolute()
            and path.parent == WORKSPACE_ROOT / "system" / "supervisord.conf.d"
        ), pattern
        included.append(_REPO_ROOT / "system" / "supervisord.conf.d" / path.name)
    return included


def test_sandbox_supervisord_runs_exactly_the_screen_the_gateway_and_owner_exec() -> (
    None
):
    dropins = _included_dropins()
    declared: set[str] = set()
    for dropin in dropins:
        assert dropin.is_file(), dropin
        for kind, name in _SECTION_PATTERN.findall(dropin.read_text()):
            if kind != "group":
                declared.add(name)
    assert declared == _SANDBOX_PROGRAMS
    # The Screen's stack group names only members the sandbox's config also declares.
    for dropin in dropins:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(dropin)
        for section in parser.sections():
            if section.startswith("group:"):
                assert set(parser[section]["programs"].split(",")) <= declared, section


def test_sandbox_supervisord_names_no_program_outside_the_workspaces_dropins() -> None:
    text = (_REPO_ROOT / SUPERVISORD_CONF_SOURCE).read_text()
    assert not _SECTION_PATTERN.search(text)


def test_boot_script_parses_runs_strict_and_names_the_contract_paths() -> None:
    boot_script = _REPO_ROOT / BOOT_SCRIPT_SOURCE
    text = boot_script.read_text()
    assert text.startswith("#!/bin/bash\n")
    assert "set -euo pipefail" in text
    assert (
        subprocess.run(
            ["bash", "-n", str(boot_script)],
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 0
    )
    assert f'readonly RUNTIME_DIR="{RUNTIME_DIR}"' in text
    assert f'readonly INSTALL_DIR="{INSTALL_DIR}"' in text
    assert f'readonly SCREEN_URL="{SCREEN_SERVICE_URL}"' in text
    assert f"${{{HOST_ID_ENV_VAR}:?" in text
    assert 'export MNGR_HOST_DIR="$RUNTIME_DIR/host"' in text
    assert 'supervisord -n -c "$INSTALL_DIR/supervisord.conf"' in text
    assert '-X POST "$SCREEN_URL/api/start"' in text
