"""The toolchain pins ``setup_system.sh`` defaults, and the harness pins that must agree with them.

Every version the script expands must have a default in the script itself. Only
the Dockerfile supplies those pins as ``ENV``; the Lima and Modal providers run
the script directly, under ``set -u``. An undefaulted version aborts those
creates with "unbound variable" while docker stays green.

Each harness binary the script bakes into the image is pinned a second time, as
its agent type's ``version`` in ``.mngr/settings.toml``, which mngr checks the
installed binary against on every create.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[1]
_SETUP_SYSTEM_PATH = _REPO_ROOT / "system" / "scripts" / "setup_system.sh"
_SETTINGS_PATH = _REPO_ROOT / ".mngr" / "settings.toml"

_EXPANSION_PATTERN = re.compile(r"\$\{(\w+_VERSION)\}")
_DEFAULT_PATTERN = re.compile(r'^: "\$\{(\w+_VERSION):=([^}]*)\}"', re.MULTILINE)

# Each harness agent type in .mngr/settings.toml, and the setup_system.sh default that
# installs its binary.
_HARNESS_PIN_VARIABLES = {
    "claude": "CLAUDE_CODE_VERSION",
    "codex": "CODEX_VERSION",
    "pi-coding": "PI_VERSION",
    "opencode": "OPENCODE_VERSION",
}


def test_every_expanded_version_has_a_setup_system_default() -> None:
    setup_system = _SETUP_SYSTEM_PATH.read_text()

    expanded = set(_EXPANSION_PATTERN.findall(setup_system))
    assert expanded, (
        f"parsed no ${{NAME_VERSION}} expansions out of {_SETUP_SYSTEM_PATH}"
    )

    defaulted = {name for name, _ in _DEFAULT_PATTERN.findall(setup_system)}
    undefaulted = sorted(expanded - defaulted)
    assert not undefaulted, (
        f"{undefaulted} are expanded by {_SETUP_SYSTEM_PATH.name} with no "
        f': "${{NAME:=...}}" default. Lima and Modal run that script without the '
        'Dockerfile\'s ENV, so it would abort there with "unbound variable".'
    )


@pytest.mark.parametrize(
    ("agent_type", "setup_variable"), sorted(_HARNESS_PIN_VARIABLES.items())
)
def test_harness_version_pin_matches_the_baked_binary_and_only_warns(
    agent_type: str, setup_variable: str
) -> None:
    """A pin that disagrees with the baked binary warns on every create of that harness.

    ``version_mismatch`` must be ``WARN`` because a workspace's binary can still drift from
    the pin, and a create that failed on it would block the update chat that repairs it.
    """
    setup_defaults = dict(_DEFAULT_PATTERN.findall(_SETUP_SYSTEM_PATH.read_text()))
    agent_config = tomllib.loads(_SETTINGS_PATH.read_text())["agent_types"][agent_type]

    assert agent_config.get("version") == setup_defaults[setup_variable], (
        f"agent_types.{agent_type}.version in {_SETTINGS_PATH.name} must equal the "
        f"{setup_variable} default in {_SETUP_SYSTEM_PATH.name}"
    )
    assert agent_config.get("version_mismatch") == "WARN"
