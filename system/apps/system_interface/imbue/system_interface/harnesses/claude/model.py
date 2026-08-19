"""Claude Code's model catalog and its model resolver.

The one place Claude's model bar behavior lives: the static catalog (the models
``claude --model`` accepts, their labels, effort set, and fast support), and the
:class:`ClaudeModelResolver` that applies a change by sending Claude Code the
``/model`` / ``/effort`` / ``/fast`` slash commands. The live READ is
harness-neutral: Claude's statusline script writes the uniform
``model_state.json`` at the agent state-dir root, which the shared reader
parses; this resolver never reads it.

Claude Code exposes no stable programmatic model list, so the catalog is
maintained by hand to match the aliases ``claude --model`` accepts. Opus uses the
``[1m]`` variant to keep the 1M-token context window the workspace provisions; fast
mode is an Opus-only capability. The ``ultra`` effort (ultracode) is declared but
hidden from the picker -- valid and matchable if a live read reports it, never
offered. Each option's ``harness_reported_model_id`` is the suffix-free API id
(``claude-opus-5``), matched against a live read. Opus launched as ``opus[1m]`` reports
the suffix too (``claude-opus-5[1m]``), which reaches the same option through
:func:`match_option`'s prefix pass.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_claude.claude_config import get_agent_hook_settings_path
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

CLAUDE_CATALOG: HarnessCatalog = HarnessCatalog(
    options=(
        ModelOption(
            id="opus[1m]",
            label="Opus 5 (1M)",
            efforts=_CLAUDE_EFFORTS,
            supports_fast=True,
            harness_reported_model_id="claude-opus-5",
        ),
        ModelOption(
            id="sonnet",
            label="Sonnet 5",
            efforts=_CLAUDE_EFFORTS,
            supports_fast=False,
            harness_reported_model_id="claude-sonnet-5",
        ),
        ModelOption(
            id="haiku",
            label="Haiku 4.5",
            efforts=_CLAUDE_EFFORTS,
            supports_fast=False,
            harness_reported_model_id="claude-haiku-4-5",
        ),
    ),
    switch_mode=SwitchMode.EAGER_THEN_RECONCILE,
    picker_mode=PickerMode.LIST,
    # No credit for claude: the harness declares an empty string, so nothing renders.
    powered_by_text="",
    # The "Shoulder tap" flushes claude's queue natively (a meta+q -> chat:cancel chord
    # delivered via mngr) instead of the SIGKILL-restart base path. See harnesses/claude/tap.py.
    native_atomic_shoulder_tap_possible=True,
)

# The statusline writes model_state.json at the agent state-dir root; the registry
# wires this as the harness's model_state_relative_path (the shared reader reads there).
CLAUDE_STATE_RELATIVE_PATH: Path = Path(".")


class FastModeSettingsError(RuntimeError):
    """Raised when an agent's Claude settings file cannot be updated safely."""


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
    """Switches a Claude agent's model/effort/fast selection (the live read is shared)."""

    _config_dir: Path
    _state_dir: Path

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "ClaudeModelResolver":
        self = cls.__new__(cls)
        self._config_dir = agent_info.claude_config_dir
        self._state_dir = agent_info.agent_state_dir
        return self

    def switch(
        self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]
    ) -> SwitchResult:
        # Model, effort, and fast are three distinct Claude Code commands. Send only
        # the axes the click actually changed -- the frontend computes that against
        # the value the user saw (the optimistic overlay), so a fast toggle does not
        # re-issue /model and /effort, AND re-picking the value you started on
        # (medium -> xhigh -> medium) still sends /effort medium. Diffing here against
        # disk instead would drop that second change whenever disk had not yet reflected
        # the first. Each command lands in the session; the statusline mirrors the
        # effective state to model_state.json, and the watch fires a fresh recompute.
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
        # The statusline writes the effective {model, effort, fast} on its next fire; the
        # frontend's optimistic overlay covers the gap until then (no UI-side state write).
        return SwitchResult(ok=True)
