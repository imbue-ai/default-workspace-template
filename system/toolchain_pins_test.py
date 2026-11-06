"""``setup_system.sh`` is the single source of the toolchain pins, and this holds
other sites to it.

Nothing else supplies the versions it expands (the Dockerfile carries no pins),
and every provider runs it under ``set -u``, so each needs a default in the
script itself. CI installs uv and Node too, and must install the ones workspaces
run: a different uv writes ``uv.lock`` differently, so the lock CI checks would
not be the lock a workspace keeps.

Each harness binary the script bakes into the image is pinned a second time, as
its agent type's ``version`` in ``.mngr/settings.toml``, which mngr checks the
installed binary against on every create.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).parents[1]
_SETUP_SYSTEM_PATH = _REPO_ROOT / "system" / "scripts" / "setup_system.sh"
_SETTINGS_PATH = _REPO_ROOT / ".mngr" / "settings.toml"
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"

_EXPANSION_PATTERN = re.compile(r"\$\{(\w+_VERSION)\}")
_DEFAULT_PATTERN = re.compile(r'^: "\$\{(\w+_VERSION):=([^}]*)\}"', re.MULTILINE)
_COMMIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")

_SETUP_UV = "astral-sh/setup-uv"
_SETUP_NODE = "actions/setup-node"

# Each harness agent type in .mngr/settings.toml, and the setup_system.sh default that
# installs its binary.
_HARNESS_PIN_VARIABLES = {
    "claude": "CLAUDE_CODE_VERSION",
    "codex": "CODEX_VERSION",
    "pi-coding": "PI_VERSION",
    "opencode": "OPENCODE_VERSION",
}


def _setup_system_default(name: str) -> str:
    defaults = dict(_DEFAULT_PATTERN.findall(_SETUP_SYSTEM_PATH.read_text()))
    assert name in defaults, f"no {name} default in {_SETUP_SYSTEM_PATH}"
    return defaults[name]


def _action_steps(action: str) -> list[tuple[str, str, dict[str, Any]]]:
    """``(workflow file, ref, with)`` for every workflow step that uses ``action``."""
    found: list[tuple[str, str, dict[str, Any]]] = []
    for workflow_path in sorted(_WORKFLOWS_DIR.glob("*.yml")):
        workflow = yaml.safe_load(workflow_path.read_text())
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                uses = step.get("uses", "")
                name, _, ref = uses.partition("@")
                if name == action:
                    found.append((workflow_path.name, ref, step.get("with") or {}))
    assert found, f"no workflow under {_WORKFLOWS_DIR} uses {action}"
    return found


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
        f': "${{NAME:=...}}" default. Nothing else supplies them, so the script '
        'aborts with "unbound variable".'
    )


def test_ci_installs_the_uv_workspaces_run() -> None:
    uv_version = _setup_system_default("UV_VERSION")
    for workflow, _, inputs in _action_steps(_SETUP_UV):
        assert inputs.get("version") == uv_version, (
            f"{workflow} installs uv {inputs.get('version')!r}; workspaces run "
            f"UV_VERSION {uv_version} from {_SETUP_SYSTEM_PATH.name}"
        )


def test_ci_installs_the_node_workspaces_run() -> None:
    node_version = _setup_system_default("NODE_VERSION")
    for workflow, _, inputs in _action_steps(_SETUP_NODE):
        assert inputs.get("node-version") == node_version, (
            f"{workflow} installs Node {inputs.get('node-version')!r}; workspaces "
            f"run NODE_VERSION {node_version} from {_SETUP_SYSTEM_PATH.name}"
        )


def test_every_setup_uv_reference_is_one_commit() -> None:
    refs = {ref for _, ref, _ in _action_steps(_SETUP_UV)}
    assert len(refs) == 1, f"{_SETUP_UV} is referenced at several refs: {sorted(refs)}"
    (ref,) = refs
    assert _COMMIT_SHA_PATTERN.fullmatch(ref), (
        f"{_SETUP_UV}@{ref} is not pinned to a full commit SHA"
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
    agent_config = tomllib.loads(_SETTINGS_PATH.read_text())["agent_types"][agent_type]

    assert agent_config.get("version") == _setup_system_default(setup_variable), (
        f"agent_types.{agent_type}.version in {_SETTINGS_PATH.name} must equal the "
        f"{setup_variable} default in {_SETUP_SYSTEM_PATH.name}"
    )
    assert agent_config.get("version_mismatch") == "WARN"
