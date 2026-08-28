"""Every toolchain version the Dockerfile pins must also have a default in ``setup_system.sh``.

``setup_system.sh`` runs two ways: the Dockerfile ``RUN``s it (docker /
vps_docker / ovh providers), and the Lima and Modal providers run it directly in
a fresh VM as an extra provision command. Only the first supplies the
Dockerfile's ``ENV``, and the script runs under ``set -u`` -- so a version
referenced by the script but pinned *only* as a Dockerfile ``ARG`` aborts every
non-docker create with "unbound variable" while docker CI stays green.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[1]
_DOCKERFILE_PATH = _REPO_ROOT / "system" / "Dockerfile"
_SETUP_SYSTEM_PATH = _REPO_ROOT / "system" / "scripts" / "setup_system.sh"

_DOCKERFILE_ARG_PATTERN = re.compile(
    r'^ARG\s+([A-Z0-9_]+_VERSION)="?([^"\s]+)"?$', re.MULTILINE
)
_SETUP_SYSTEM_DEFAULT_PATTERN = re.compile(
    r'^:\s+"\$\{([A-Z0-9_]+_VERSION):=([^"}]+)\}"$', re.MULTILINE
)


def _dockerfile_version_args() -> dict[str, str]:
    return dict(_DOCKERFILE_ARG_PATTERN.findall(_DOCKERFILE_PATH.read_text()))


def _setup_system_version_defaults() -> dict[str, str]:
    return dict(_SETUP_SYSTEM_DEFAULT_PATTERN.findall(_SETUP_SYSTEM_PATH.read_text()))


def test_dockerfile_version_args_have_setup_system_defaults() -> None:
    dockerfile_args = _dockerfile_version_args()
    assert dockerfile_args, (
        f"parsed no `ARG <NAME>_VERSION=` lines out of {_DOCKERFILE_PATH}"
    )

    missing = sorted(set(dockerfile_args) - set(_setup_system_version_defaults()))
    assert not missing, (
        f"{missing} are pinned in {_DOCKERFILE_PATH.name} but have no "
        f': "${{NAME:=...}}" default in {_SETUP_SYSTEM_PATH.name}. Lima and Modal '
        "run that script without the Dockerfile's ENV, so it would abort with "
        '"unbound variable" there.'
    )


def test_dockerfile_version_args_match_setup_system_defaults() -> None:
    setup_system_defaults = _setup_system_version_defaults()
    assert setup_system_defaults, (
        f'parsed no `: "${{NAME:=...}}"` lines out of {_SETUP_SYSTEM_PATH}'
    )

    mismatched = {
        name: (dockerfile_version, setup_system_defaults[name])
        for name, dockerfile_version in _dockerfile_version_args().items()
        if name in setup_system_defaults
        and setup_system_defaults[name] != dockerfile_version
    }
    assert not mismatched, (
        f"{mismatched} disagree between {_DOCKERFILE_PATH.name} and "
        f"{_SETUP_SYSTEM_PATH.name} (name: (dockerfile, setup_system)). Docker "
        "images and Lima/Modal VMs would install different versions."
    )
