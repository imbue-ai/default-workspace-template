"""Claude Code's model catalog and its model resolver.

The one place Claude's model bar behavior lives: the static catalog (the models
``claude --model`` accepts, their labels, effort set, and fast support), and the
:class:`ClaudeModelResolver` that reads the agent's current selection from its
``settings.json`` and applies a change by sending Claude Code the ``/model`` /
``/effort`` / ``/fast`` slash commands. This absorbs what used to live in the
Claude-specific ``model_settings.py`` and the per-agent half of ``fast_mode.py``;
the workspace launch-default (``launch_defaults.py``) is a separate concern the
resolver never touches.

Claude Code exposes no stable programmatic model list, so the catalog is
maintained by hand to match the aliases ``claude --model`` accepts. Opus uses the
``[1m]`` variant to keep the 1M-token context window the workspace provisions; fast
mode is an Opus-only capability. The ``ultra`` effort (ultracode) is declared but
hidden from the picker -- valid and matchable if a live read reports it, never
offered.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.file_utils import read_json_dict
from imbue.mngr_claude.claude_config import get_agent_hook_settings_path
from imbue.mngr_claude.claude_config import get_managed_settings_path
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.model import EffortChoice
from imbue.system_interface.harnesses.model import HarnessCatalog
from imbue.system_interface.harnesses.model import HarnessModelResolver
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.model import PickerMode
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.model import SwitchResult
from imbue.system_interface.harnesses.model import base_alias
from imbue.system_interface.harnesses.model import parse_effort_level

# The snapshot the Claude model-state hook writes ({model, effort, fast}): the live model +
# effective fast (from the transcript's service tier) + effort (from settings). This is the
# authoritative live read for the model bar; settings.json only records the fast *preference*,
# which lies when fast is unavailable at runtime. Kept in sync with resources/model_state_hook.py.
_MODEL_STATE_NAME: str = "claude_model_state.json"


def _to_catalog_model_id(raw: str) -> str:
    """Map a raw model id to a catalog option id so ``match_option`` resolves it.

    The hook records whatever the transcript says -- the API id (``claude-opus-4-8``) at Stop,
    or the settings alias (``opus[1m]``) before a turn. Both should light the same chip, so we
    match on the catalog alias appearing in the raw id (``opus`` in ``claude-opus-4-8`` and in
    ``opus[1m]``). Falls back to the raw string (a shrug) when nothing matches.
    """
    lowered = raw.lower()
    for option in CLAUDE_CATALOG.options:
        if base_alias(option.id) in lowered:
            return option.id
    return raw

# Every Claude model offers the same efforts: low..max shown, ultra (ultracode)
# declared-but-hidden. Effort levels are plain strings, as the catalog carries them.
_CLAUDE_EFFORTS: tuple[EffortChoice, ...] = (
    EffortChoice(level="low"),
    EffortChoice(level="medium"),
    EffortChoice(level="high"),
    EffortChoice(level="xhigh"),
    EffortChoice(level="max"),
    EffortChoice(level="ultra", in_picker=False),
)

# The effort a guess falls back to when launch config records none yet.
_DEFAULT_EFFORT: str = "medium"

CLAUDE_CATALOG: HarnessCatalog = HarnessCatalog(
    options=(
        ModelOption(id="opus[1m]", label="Opus 5 (1M)", efforts=_CLAUDE_EFFORTS, supports_fast=True),
        ModelOption(id="fable", label="Fable 5", efforts=_CLAUDE_EFFORTS, supports_fast=False),
        ModelOption(id="sonnet", label="Sonnet 5", efforts=_CLAUDE_EFFORTS, supports_fast=False),
        ModelOption(id="haiku", label="Haiku 4.5", efforts=_CLAUDE_EFFORTS, supports_fast=False),
    ),
    default_model_id="opus[1m]",
    switch_mode=SwitchMode.EAGER_THEN_RECONCILE,
    picker_mode=PickerMode.LIST,
    powered_by_label="Claude Code",
)


class FastModeSettingsError(RuntimeError):
    """Raised when an agent's Claude settings file cannot be updated safely."""


def _read_fast_mode_setting(settings_path: Path) -> bool | None:
    """The ``fastMode`` value in a Claude Code settings file, or None when unset.

    Absent and present-but-false are genuinely different here: Claude Code deletes
    the key when ``/fast`` turns fast mode off rather than writing false, so only a
    caller that knows the layering can decide what an absent key means. A missing
    file reads as unset silently; a corrupt one reads as unset but is logged. Kept
    bespoke (not ``read_json_dict``) precisely to preserve the absent-vs-corrupt
    distinction the two-layer merge depends on.
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


def _resolve_agent_fast_mode(claude_settings_path: Path, managed_settings_path: Path) -> bool:
    """Whether fast mode is on for the agent, across the two settings layers.

    Claude Code layers mngr's managed ``--settings`` file at command-line
    precedence, above the shared user settings, so a ``fastMode`` set there wins.
    Only when the managed file leaves it unset does the user settings file decide,
    and an absent key there means off. This is also what the agent would come back
    with if it restarted, because every change made through the UI is written into
    the same per-agent file (see ``_write_fast_mode_setting``).
    """
    managed_setting = _read_fast_mode_setting(managed_settings_path)
    if managed_setting is not None:
        return managed_setting
    user_setting = _read_fast_mode_setting(claude_settings_path)
    if user_setting is not None:
        return user_setting
    return False


def _get_agent_fast_mode_write_path(claude_config_dir: Path, agent_state_dir: Path) -> Path:
    """The per-agent settings file a fast-mode change must be recorded in.

    mngr keeps each agent's launch settings under its state dir and re-applies them
    on every launch, so recording a change there is what makes it outlive a restart.
    Which file that is depends on the config mode, and mngr's own helper owns that
    branch: shared mode gets the managed ``--settings`` overlay, isolated mode gets
    the per-agent config dir's ``settings.json``. The mode is read off whether the
    agent's config dir is its own (inside the state dir) or the host-wide shared one,
    because writing fast mode into the shared config dir would set it for every agent.
    """
    is_config_dir_shared = not claude_config_dir.is_relative_to(agent_state_dir)
    return get_agent_hook_settings_path(agent_state_dir, use_env_config_dir=is_config_dir_shared)


def _read_settings_object(settings_path: Path) -> dict[str, Any]:
    """The settings file's contents as a mutable dict; empty when it does not exist."""
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


def _write_fast_mode_setting(settings_path: Path, is_enabled: bool) -> None:
    """Record ``fastMode`` in a Claude Code settings file, leaving other keys intact.

    This is the only durable record of the setting: Claude Code deletes the
    ``fastMode`` key on ``/fast off`` rather than writing false, so the session's own
    state is not recoverable from what it writes. mngr owns the file this targets and
    holds its hooks, hence a patch of one key rather than a replacement. Raises
    ``FastModeSettingsError`` when the file exists but is not a JSON object.
    """
    settings = _read_settings_object(settings_path)
    settings["fastMode"] = is_enabled
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(settings_path, json.dumps(settings))


class ClaudeModelResolver(HarnessModelResolver):
    """Reads and switches a Claude agent's model/effort/fast selection."""

    _config_dir: Path
    _state_dir: Path
    _settings_path: Path
    _managed_path: Path

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "ClaudeModelResolver":
        self = cls.__new__(cls)
        self._config_dir = agent_info.claude_config_dir
        self._state_dir = agent_info.agent_state_dir
        self._settings_path = agent_info.claude_config_dir / "settings.json"
        self._managed_path = get_managed_settings_path(agent_info.agent_state_dir)
        return self

    def guess_from_launch(self) -> ModelIdentity:
        data = read_json_dict(self._settings_path)
        model = data.get("model")
        model_id = model if isinstance(model, str) and model else CLAUDE_CATALOG.default_model_id
        effort = parse_effort_level(data.get("effortLevel")) or _DEFAULT_EFFORT
        fast = _resolve_agent_fast_mode(self._settings_path, self._managed_path)
        return ModelIdentity(model_id=model_id, effort=effort, fast=fast)

    def read_live(self) -> ModelIdentity | None:
        # The model-state hook writes the live {model, effort, fast} to a snapshot -- the only
        # source that knows the EFFECTIVE fast (settings.json's fastMode is just the preference
        # and lies when fast is unavailable at runtime). Prefer it; the model id is mapped to a
        # catalog option (it may be a raw API id from the transcript).
        snapshot = read_json_dict(self._state_dir / _MODEL_STATE_NAME)
        snapshot_model = snapshot.get("model")
        if isinstance(snapshot_model, str) and snapshot_model:
            return ModelIdentity(
                model_id=_to_catalog_model_id(snapshot_model),
                effort=parse_effort_level(snapshot.get("effort")),
                fast=snapshot.get("fast") is True,
            )
        # No snapshot yet (before the first hook fires, or an older agent): fall back to
        # settings.json for model + effort + the fast preference.
        data = read_json_dict(self._settings_path)
        model = data.get("model")
        if not isinstance(model, str) or not model:
            return None
        effort = parse_effort_level(data.get("effortLevel"))
        fast = _resolve_agent_fast_mode(self._settings_path, self._managed_path)
        return ModelIdentity(model_id=model, effort=effort, fast=fast)

    def _write_model_state_snapshot(self, identity: ModelIdentity) -> None:
        """Optimistically record a switch into the snapshot so the bar reconciles at once.

        A bar-driven switch applies via slash commands, but no hook fires until the next
        turn/submit, so without this the reconcile would read a stale snapshot and revert the
        chip. The hook overwrites this with the effective state at the next Stop.
        """
        path = self._state_dir / _MODEL_STATE_NAME
        try:
            atomic_write(path, json.dumps({"model": identity.model_id, "effort": identity.effort, "fast": identity.fast}))
        except OSError as e:
            logger.warning("Failed to write Claude model-state snapshot at {}: {}", path, e)

    def watched_paths(self) -> tuple[Path, ...]:
        return (self._settings_path, self._managed_path, self._state_dir / _MODEL_STATE_NAME)

    def switch(
        self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]
    ) -> SwitchResult:
        # Model, effort, and fast are three distinct Claude Code commands. Send only
        # the axes the click actually changed -- the frontend computes that against
        # the value the user saw (the optimistic overlay), so a fast toggle does not
        # re-issue /model and /effort, AND re-picking the value you started on
        # (medium -> xhigh -> medium) still sends /effort medium. Diffing here against
        # settings.json instead would drop that second change whenever disk had not
        # yet reflected the first. Each command sent mutates settings.json, and the
        # watch fires a fresh recompute.
        if ModelAxis.MODEL in axes:
            if not send(f"/model {identity.model_id}"):
                return SwitchResult(ok=False, detail="Failed to deliver /model to the agent")
        if ModelAxis.EFFORT in axes and identity.effort is not None:
            if not send(f"/effort {identity.effort}"):
                return SwitchResult(ok=False, detail="Failed to deliver /effort to the agent")
        if ModelAxis.FAST in axes:
            if not send("/fast on" if identity.fast else "/fast off"):
                return SwitchResult(ok=False, detail="Failed to deliver /fast to the agent")
            # Claude Code leaves no durable record of fast off, so record it into the
            # agent's launch settings -- that is what a restart comes back with.
            write_path = _get_agent_fast_mode_write_path(self._config_dir, self._state_dir)
            try:
                _write_fast_mode_setting(write_path, identity.fast)
            except (FastModeSettingsError, OSError) as e:
                logger.opt(exception=e).error("Failed to record fast mode at {}", write_path)
                return SwitchResult(ok=False, detail="Applied the change but could not record fast mode")
        # Reflect the pick in the snapshot immediately so the chip reconciles without waiting
        # for the next hook fire; the hook re-writes the effective state at the next Stop.
        self._write_model_state_snapshot(identity)
        return SwitchResult(ok=True)
