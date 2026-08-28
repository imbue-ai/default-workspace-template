"""Every version ``setup_system.sh`` expands must have a default in the script itself.

The script runs two ways: the Dockerfile ``RUN``s it (docker / vps_docker / ovh
providers), and the Lima and Modal providers run it directly in a fresh VM as an
extra provision command. Only the first supplies the Dockerfile's ``ENV``, and
the script runs under ``set -u`` -- so a version it expands without a ``:=``
default aborts every non-docker create with "unbound variable" while docker CI
stays green.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[1]
_SETUP_SYSTEM_PATH = _REPO_ROOT / "system" / "scripts" / "setup_system.sh"

_EXPANSION_PATTERN = re.compile(r"\$\{(\w+_VERSION)\}")
_DEFAULT_PATTERN = re.compile(r'^: "\$\{(\w+_VERSION):=', re.MULTILINE)


def test_every_expanded_version_has_a_setup_system_default() -> None:
    setup_system = _SETUP_SYSTEM_PATH.read_text()

    expanded = set(_EXPANSION_PATTERN.findall(setup_system))
    assert expanded, (
        f"parsed no ${{NAME_VERSION}} expansions out of {_SETUP_SYSTEM_PATH}"
    )

    undefaulted = sorted(expanded - set(_DEFAULT_PATTERN.findall(setup_system)))
    assert not undefaulted, (
        f"{undefaulted} are expanded by {_SETUP_SYSTEM_PATH.name} with no "
        f': "${{NAME:=...}}" default. Lima and Modal run that script without the '
        'Dockerfile\'s ENV, so it would abort there with "unbound variable".'
    )
