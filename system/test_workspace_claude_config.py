"""Pin the workspace's Claude configuration contract after the ~/.claude cutover.

Every claude in a workspace (mngr-launched agents and a bare ``claude`` in a
terminal alike) must resolve claude's own default config dir, the shared
``~/.claude``. That relies on invariants spread across files that would
otherwise drift silently:

1. Nothing in ``.mngr/settings.toml`` may export ``CLAUDE_CONFIG_DIR`` -- an
   exported value would pin agents to a different dir than a bare ``claude``.
2. The ``main`` (services) agent type must resolve to the plain ``command``
   agent class, NOT a claude agent: a claude-typed services agent pins
   ``CLAUDE_CONFIG_DIR`` to its per-agent dir in its env, and everything it
   spawns (supervisord services, the bootstrap's / system_interface's
   ``mngr create`` calls) would inherit that pin.
3. The Claude Code version is pinned in three places (the Dockerfile ARG, the
   ``setup_system.sh`` default, and ``agent_types.claude.version``) that must
   agree. Since the services agent is no longer a claude agent, mngr's runtime
   pin check only fires when the first chat agent is created on first boot --
   ``setup_system.sh`` fails the build on an installer mismatch, and this test
   catches a desync between the three pinned values at merge time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import tomlkit
from imbue.mngr.agents.agent_registry import load_agents_from_plugins
from imbue.mngr.agents.default_plugins.command_agent import CommandAgent
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import AgentTypeConfig, MngrConfig
from imbue.mngr.main import get_or_create_plugin_manager
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr_claude.plugin import ClaudeAgentConfig

_REPO_ROOT = Path(__file__).parents[1]
_SETTINGS_PATH = _REPO_ROOT / ".mngr" / "settings.toml"
_DOCKERFILE_PATH = _REPO_ROOT / "system" / "Dockerfile"
_SETUP_SYSTEM_PATH = _REPO_ROOT / "system" / "scripts" / "setup_system.sh"


def _load_raw_settings() -> dict[str, Any]:
    return tomlkit.parse(_SETTINGS_PATH.read_text()).unwrap()


def _iter_leaf_strings(value: Any) -> list[str]:
    """Flatten a parsed TOML structure into its leaf string values."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [leaf for child in value.values() for leaf in _iter_leaf_strings(child)]
    if isinstance(value, (list, tuple)):
        return [leaf for child in value for leaf in _iter_leaf_strings(child)]
    return []


def test_settings_toml_does_not_export_claude_config_dir() -> None:
    """No env list, setting, or command in settings.toml may mention CLAUDE_CONFIG_DIR.

    The whole point of the ~/.claude cutover is that the var stays unset
    workspace-wide; a single host_env/env entry would silently re-pin every
    agent on the host.
    """
    leaves = _iter_leaf_strings(_load_raw_settings())
    offending = [leaf for leaf in leaves if "CLAUDE_CONFIG_DIR" in leaf]
    assert offending == []


def test_main_agent_type_resolves_to_plain_command_agent() -> None:
    """The services agent must be a `command` agent with a bare sleep, not a claude.

    Also validates (via model_validate) that the [agent_types.main] block
    carries only base AgentTypeConfig fields -- claude-only fields left behind
    from the old claude-parented block would fail config loading at boot.
    """
    raw_main = _load_raw_settings()["agent_types"]["main"]
    parsed_main = AgentTypeConfig.model_validate(raw_main)

    pm = get_or_create_plugin_manager()
    load_agents_from_plugins(pm)
    config = MngrConfig(agent_types={AgentTypeName("main"): parsed_main})
    resolved = resolve_agent_type(AgentTypeName("main"), config)

    assert resolved.agent_class is CommandAgent
    assert not isinstance(resolved.agent_config, ClaudeAgentConfig)
    assert str(resolved.agent_config.command) == "sleep infinity"


def test_claude_version_pin_is_consistent_across_settings_dockerfile_and_setup_script() -> None:
    settings_version = _load_raw_settings()["agent_types"]["claude"]["version"]

    dockerfile_match = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", _DOCKERFILE_PATH.read_text(), re.MULTILINE)
    assert dockerfile_match is not None
    dockerfile_version = dockerfile_match.group(1)

    setup_match = re.search(r"\$\{CLAUDE_CODE_VERSION:=(\S+)\}", _SETUP_SYSTEM_PATH.read_text())
    assert setup_match is not None
    setup_version = setup_match.group(1)

    assert settings_version == dockerfile_version
    assert settings_version == setup_version
