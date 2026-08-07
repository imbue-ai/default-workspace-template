"""Codex's model catalog and its (read-only, v1) model resolver.

Codex's model/effort/fast selection lives inside its transcript: every time the
selection changes, codex appends a ``thread_settings_applied`` event to the live
rollout. This resolver reads the LAST such event to show the agent's current
selection, and follows launch config (``config.toml``) for the pre-turn guess.

Switching is display-only for now (:attr:`SwitchMode.READ_ONLY`): typing
``/model <slug> <effort>`` into today's codex TUI opens the interactive picker
modal and wedges the pane, so ``switch`` sends nothing and reports that switching
is unavailable. Once the upstream one-shot ``/model`` patch lands, this becomes a
one-line send and the catalog's ``switch_mode`` flips -- the seam is already here.

It reads the rollout INDEPENDENTLY of the session watcher (which tails the same
file for the transcript): two read-only cursors, by design, so model resolution
stays decoupled from transcript tailing. The marker-read that finds which rollout
is live is the one thing they share (:func:`resolve_active_rollout_path`).
"""

import json
import threading
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.mngr_codex.codex_config import get_codex_config_path
from imbue.mngr_codex.codex_config import get_codex_home
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.codex.watcher import codex_sessions_dir
from imbue.system_interface.harnesses.codex.watcher import resolve_active_rollout_path
from imbue.system_interface.harnesses.model import EffortChoice
from imbue.system_interface.harnesses.model import HarnessCatalog
from imbue.system_interface.harnesses.model import HarnessModelResolver
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.model import PickerMode
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.model import SwitchResult
from imbue.system_interface.harnesses.model import parse_effort_level

# Codex efforts: low..xhigh shown; max/ultra declared-but-hidden (valid + matchable,
# never offered). Plain strings, as the catalog carries them.
_CODEX_EFFORTS: tuple[EffortChoice, ...] = (
    EffortChoice(level="low"),
    EffortChoice(level="medium"),
    EffortChoice(level="high"),
    EffortChoice(level="xhigh"),
    EffortChoice(level="max", in_picker=False),
    EffortChoice(level="ultra", in_picker=False),
)

_DEFAULT_EFFORT: str = "medium"

CODEX_CATALOG: HarnessCatalog = HarnessCatalog(
    options=(
        ModelOption(id="gpt-5.6-sol", label="GPT-5.6-Sol", efforts=_CODEX_EFFORTS, supports_fast=True),
        ModelOption(id="gpt-5.6-terra", label="GPT-5.6-Terra", efforts=_CODEX_EFFORTS, supports_fast=True),
        ModelOption(id="gpt-5.6-luna", label="GPT-5.6-Luna", efforts=_CODEX_EFFORTS, supports_fast=True),
        ModelOption(id="gpt-5.5", label="GPT-5.5", efforts=_CODEX_EFFORTS, supports_fast=True),
        ModelOption(id="gpt-5.2", label="GPT-5.2", efforts=_CODEX_EFFORTS, supports_fast=True),
    ),
    default_model_id="gpt-5.6-sol",
    # ON_CHANGE (not EAGER): codex applies a switch through its CLI and records it to
    # the rollout, which the watcher reconciles into the chip. We do not move the chip
    # optimistically -- it follows the rollout truth once the command lands. The patched
    # codex binary (see setup_system.sh) makes /model <model> [effort] apply inline; the
    # unpatched binary silently ignored it, which is why this used to be READ_ONLY.
    switch_mode=SwitchMode.ON_CHANGE,
    picker_mode=PickerMode.LIST,
    icon_svg=(Path(__file__).parent / "icon.svg").read_text(),
)


def _thread_settings_from_line(line: str) -> dict[str, Any] | None:
    """The ``thread_settings`` dict from a ``thread_settings_applied`` rollout line,
    or None for any other line (or unparseable JSON)."""
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict) or record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "thread_settings_applied":
        return None
    settings = payload.get("thread_settings")
    return settings if isinstance(settings, dict) else None


def _identity_from_thread_settings(settings: dict[str, Any] | None) -> ModelIdentity | None:
    """Turn a codex ``thread_settings`` dict into a :class:`ModelIdentity`, or None."""
    if settings is None:
        return None
    model = settings.get("model")
    if not isinstance(model, str) or not model:
        return None
    effort = parse_effort_level(settings.get("reasoning_effort"))
    # ``priority`` is codex's fast tier; anything else (``default``, absent) is off.
    fast = settings.get("service_tier") == "priority"
    return ModelIdentity(model_id=model, effort=effort, fast=fast)


class CodexModelResolver(HarnessModelResolver):
    """Reads a codex agent's current selection from its rollout. Read-only v1."""

    _state_dir: Path
    _lock: threading.Lock
    # The rollout being tailed for thread-settings, and a byte cursor into it, so a
    # recompute reads only appended bytes rather than the whole (possibly large)
    # rollout each time. Reset on rotation (a new rollout on resume).
    _current_rollout: Path | None
    _offset: int
    _last_settings: dict[str, Any] | None

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "CodexModelResolver":
        self = cls.__new__(cls)
        self._state_dir = agent_info.agent_state_dir
        self._lock = threading.Lock()
        self._current_rollout = None
        self._offset = 0
        self._last_settings = None
        return self

    def guess_from_launch(self) -> ModelIdentity:
        config = self._read_config()
        model = config.get("model")
        model_id = model if isinstance(model, str) and model else CODEX_CATALOG.default_model_id
        effort = parse_effort_level(config.get("model_reasoning_effort")) or _DEFAULT_EFFORT
        # config.toml carries no service tier; a fresh agent is not on the fast tier.
        return ModelIdentity(model_id=model_id, effort=effort, fast=False)

    def read_live(self) -> ModelIdentity | None:
        rollout = resolve_active_rollout_path(self._state_dir)
        if rollout is None:
            return None
        with self._lock:
            self._consume_new_settings(rollout)
            return _identity_from_thread_settings(self._last_settings)

    def watched_paths(self) -> tuple[Path, ...]:
        # The stable sessions root, watched recursively: every rollout (across
        # rotation) writes under it, so any thread-settings append wakes the recompute.
        return (codex_sessions_dir(self._state_dir),)

    def switch(
        self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]
    ) -> SwitchResult:
        # Codex applies model and effort together via one command -- `/model <model>
        # [effort]` -- so any change to either axis is one send (the patched binary
        # applies it inline; see setup_system.sh). Fast (service_tier=priority) is codex's
        # separate /fast toggle. Only the axes the click changed are sent (see the shared
        # `switch` contract); ON_CHANGE means we do not echo an optimistic value -- the
        # rollout's thread_settings reconcile the chip once the command lands.
        if ModelAxis.MODEL in axes or ModelAxis.EFFORT in axes:
            command = f"/model {identity.model_id}"
            if identity.effort is not None:
                command = f"{command} {identity.effort}"
            if not send(command):
                return SwitchResult(ok=False, detail="Failed to deliver /model to the agent")
        if ModelAxis.FAST in axes:
            if not send("/fast on" if identity.fast else "/fast off"):
                return SwitchResult(ok=False, detail="Failed to deliver /fast to the agent")
        return SwitchResult(ok=True)

    def _read_config(self) -> dict[str, Any]:
        """The agent's codex ``config.toml`` as a dict; empty when absent/malformed."""
        config_path = get_codex_config_path(get_codex_home(self._state_dir))
        try:
            return tomllib.loads(config_path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, tomllib.TOMLDecodeError) as e:
            logger.warning("Ignored unreadable codex config at {}: {}", config_path, e)
            return {}

    def _consume_new_settings(self, rollout: Path) -> None:
        """Advance the cursor over ``rollout`` and update ``_last_settings`` from any
        new ``thread_settings_applied`` lines. Must hold ``_lock``."""
        if rollout != self._current_rollout:
            # First resolution or rotation (resume -> new rollout): tail from its start.
            self._current_rollout = rollout
            self._offset = 0
            self._last_settings = None
        try:
            size = rollout.stat().st_size
        except OSError:
            return
        # A shrink means truncation/re-materialisation: re-read from the start.
        if size < self._offset:
            self._offset = 0
            self._last_settings = None
        if size == self._offset:
            return
        try:
            with rollout.open("rb") as f:
                f.seek(self._offset)
                raw = f.read()
        except OSError as e:
            logger.debug("codex resolver: failed to read {}: {}", rollout, e)
            return
        # Only consume through the last complete line; leave a half-written trailing
        # line for the next read (splitting on b"\n" is UTF-8 safe).
        newline_index = raw.rfind(b"\n")
        if newline_index == -1:
            return
        complete = raw[: newline_index + 1]
        self._offset += len(complete)
        for line in complete.decode("utf-8", "replace").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            settings = _thread_settings_from_line(stripped)
            if settings is not None:
                self._last_settings = settings
