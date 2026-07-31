import json
import os
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.mngr_claude.claude_config import get_agent_hook_settings_path

# Machine state, so it sits under data/.state/ next to apps.toml. JSON rather than
# TOML because nothing authors it by hand -- the system interface is the only
# writer, matching the workspace's other machine-written state.
_DECISION_RELATIVE_PATH: Final[str] = "data/.state/fast_mode_decision.json"

# The key both writers of the decision file agree on. Bootstrap parses the same
# file without importing this module (it must stay dependency-free), so the format
# is deliberately one boolean and nothing else.
_DECISION_KEY: Final[str] = "is_fast_mode_enabled"

# What a chat agent launches with before the workspace has answered the prompt.
# The opening conversation runs fast so it feels responsive; the prompt then asks
# whether that is worth its higher per-token price.
FAST_MODE_BEFORE_DECISION: Final[bool] = True


class FastModeSettingsError(RuntimeError):
    """Raised when an agent's Claude settings file cannot be updated safely."""


def get_workspace_fast_mode_decision_path(workspace_work_dir: Path) -> Path:
    return workspace_work_dir / _DECISION_RELATIVE_PATH


def read_workspace_fast_mode_decision(decision_path: Path) -> bool | None:
    """The workspace's recorded fast-mode answer, or None when it has not answered.

    Undecided is the file being absent, so there is no separate "decided" flag that
    could disagree with the value. A corrupt or wrong-shaped file also reads as
    undecided -- it must not strand the workspace at a setting nobody chose -- but
    unlike an absent one it is logged, since falling back turns on the setting that
    costs money.
    """
    try:
        raw = decision_path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("Failed to read fast-mode decision at {}: {}", decision_path, e)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Ignored unparseable fast-mode decision at {}: {}", decision_path, e)
        return None
    is_enabled = data.get(_DECISION_KEY) if isinstance(data, dict) else None
    if not isinstance(is_enabled, bool):
        logger.warning("Ignored fast-mode decision at {} with no boolean {}: {}", decision_path, _DECISION_KEY, raw)
        return None
    return is_enabled


def write_workspace_fast_mode_decision(decision_path: Path, is_fast_mode_enabled: bool) -> None:
    """Record the workspace's answer, replacing any previous one."""
    _write_json_atomically(decision_path, {_DECISION_KEY: is_fast_mode_enabled})


def read_fast_mode_setting(settings_path: Path) -> bool | None:
    """The ``fastMode`` value in a Claude Code settings file, or None when it is not set.

    Absent and present-but-false are genuinely different here: Claude Code deletes
    the key when ``/fast`` turns fast mode off rather than writing false, so only a
    caller that knows the layering can decide what an absent key means.

    A file that is simply not there is the expected case for the managed overlay
    and reads as unset silently; anything else that goes wrong reading or parsing
    it also reads as unset, but is logged -- it would otherwise hand the decision
    to a lower layer with nothing to say why.
    """
    try:
        raw = settings_path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("Failed to read Claude settings at {}: {}", settings_path, e)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Ignored unparseable Claude settings at {}: {}", settings_path, e)
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("fastMode")
    return value if isinstance(value, bool) else None


def resolve_agent_fast_mode(claude_settings_path: Path, managed_settings_path: Path) -> bool:
    """Whether fast mode is on for the agent.

    Claude Code layers mngr's managed ``--settings`` file at command-line precedence,
    above the shared user settings, so a ``fastMode`` set there wins. Only when the
    managed file leaves it unset does the user settings file decide, and an absent
    key there means off.

    This is also what the agent would come back with if it restarted, because every
    change made through this UI is written into the same per-agent file (see
    ``write_fast_mode_setting``) -- the running session and the next launch cannot
    disagree.
    """
    managed_setting = read_fast_mode_setting(managed_settings_path)
    if managed_setting is not None:
        return managed_setting
    user_setting = read_fast_mode_setting(claude_settings_path)
    if user_setting is not None:
        return user_setting
    return False


def get_agent_fast_mode_write_path(claude_config_dir: Path, agent_state_dir: Path) -> Path:
    """The per-agent settings file a fast-mode change must be recorded in.

    mngr keeps each agent's launch settings under its state dir and re-applies them
    on every launch, so recording a change there is what makes it outlive a restart.
    Which file that is depends on the config mode, and mngr's own helper owns that
    branch: shared mode has no per-agent config dir and gets the managed
    ``--settings`` overlay, isolated mode gets the per-agent config dir's
    ``settings.json``.

    The mode is read off whether the agent's config dir is its own (inside the state
    dir) or the host-wide shared one, because writing fast mode into the shared
    config dir would set it for every agent on the host.
    """
    is_config_dir_shared = not claude_config_dir.is_relative_to(agent_state_dir)
    return get_agent_hook_settings_path(agent_state_dir, use_env_config_dir=is_config_dir_shared)


def write_fast_mode_setting(settings_path: Path, is_enabled: bool) -> None:
    """Record ``fastMode`` in a Claude Code settings file, leaving its other keys intact.

    This is the only durable record of the setting: Claude Code deletes the
    ``fastMode`` key on ``/fast off`` rather than writing false, so the session's own
    state is not recoverable from what it writes.

    mngr owns the file this usually targets and rewrites it fresh on provision, which
    happens at create and not on restart -- so a value written here survives for the
    life of the agent. It also holds mngr's hooks, hence a patch of one key rather
    than a replacement.

    Raises ``FastModeSettingsError`` when the file exists but does not hold a JSON
    object, rather than replacing it and silently dropping those hooks.
    """
    settings = _read_settings_object(settings_path)
    settings["fastMode"] = is_enabled
    _write_json_atomically(settings_path, settings)


def _read_settings_object(settings_path: Path) -> dict[str, Any]:
    """The settings file's contents as a mutable dict; empty when it does not exist yet."""
    try:
        raw = settings_path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise FastModeSettingsError(f"Failed to read Claude settings at {settings_path}: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise FastModeSettingsError(f"Claude settings at {settings_path} are not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise FastModeSettingsError(f"Claude settings at {settings_path} are not a JSON object")
    return data


def _write_json_atomically(path: Path, data: dict[str, Any]) -> None:
    """Replace ``path`` with ``data``, so a concurrent reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(json.dumps(data))
    os.replace(temporary_path, path)
