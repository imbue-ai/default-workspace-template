"""Codex's model catalog and its model resolver.

The patched codex ("codex-in-minds") mirrors its effective model to disk: on
every model/effort change and every service-tier change it atomically writes
``$CODEX_HOME/minds_model_state.json`` -- ``{"model", "reasoning_effort",
"service_tier"}`` -- covering framework-initiated changes too (session
configure/resume, server-pushed thread-settings, thread switches, the
out-of-usage "switch model" prompt, fast-mode toggles). The file exists from
session open (holding the launch values) and updates in well under 100ms. This
resolver reads that file for the live selection, and follows launch config
(``config.toml``) for the pre-turn guess.

This replaces an earlier reader that tailed the rollout for a
``thread_settings_applied`` event: the installed patched codex (0.146.0) no
longer emits that event (model/effort ride a ``turn_context`` payload and the
service tier is absent from the rollout), so the rollout read returned nothing.
The state file is the direct, always-current source -- the codex twin of pi's
``pi_model_state.json`` -- and lets the model bar reflect a change before the
first turn exists (when there is no rollout at all). When an unpatched codex is
run the file is absent, ``read_live`` returns ``None``, and the bar falls back to
the launch guess.
"""

import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.mngr.utils.file_utils import read_json_dict
from imbue.mngr_codex.codex_config import get_codex_config_path
from imbue.mngr_codex.codex_config import get_codex_home
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
    # EAGER_THEN_RECONCILE: the patched codex applies /model <model> [effort] inline and
    # mirrors the effective model to minds_model_state.json within ~100ms, which the
    # watcher reconciles into the chip. That write is fast and reliable enough to move the
    # chip optimistically on click and snap it to the pushed live choice a beat later
    # (the frontend's 5-minute pending fallback only fires if the switch never lands).
    switch_mode=SwitchMode.EAGER_THEN_RECONCILE,
    picker_mode=PickerMode.LIST,
    powered_by_label="Codex",
)


# The effective-model mirror the patched codex writes atomically under CODEX_HOME on
# every model/effort/tier change. Kept in sync with the codex-in-minds patch.
_MODEL_STATE_NAME: str = "minds_model_state.json"


def codex_model_state_path(agent_state_dir: Path) -> Path:
    """The effective-model mirror file for a codex agent (under its CODEX_HOME)."""
    return get_codex_home(agent_state_dir) / _MODEL_STATE_NAME


def _identity_from_model_state(data: dict[str, Any]) -> ModelIdentity | None:
    """Turn a ``minds_model_state.json`` dict into a :class:`ModelIdentity`, or None
    when no model is recorded yet."""
    model = data.get("model")
    if not isinstance(model, str) or not model:
        return None
    effort = parse_effort_level(data.get("reasoning_effort"))
    # ``priority`` is codex's fast tier; anything else (``default``, absent) is off.
    fast = data.get("service_tier") == "priority"
    return ModelIdentity(model_id=model, effort=effort, fast=fast)


class CodexModelResolver(HarnessModelResolver):
    """Reads a codex agent's current selection from its ``minds_model_state.json`` mirror."""

    _state_dir: Path

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "CodexModelResolver":
        self = cls.__new__(cls)
        self._state_dir = agent_info.agent_state_dir
        return self

    def guess_from_launch(self) -> ModelIdentity:
        config = self._read_config()
        model = config.get("model")
        model_id = model if isinstance(model, str) and model else CODEX_CATALOG.default_model_id
        effort = parse_effort_level(config.get("model_reasoning_effort")) or _DEFAULT_EFFORT
        # config.toml carries no service tier; a fresh agent is not on the fast tier.
        return ModelIdentity(model_id=model_id, effort=effort, fast=False)

    def read_live(self) -> ModelIdentity | None:
        # The patched codex writes the effective model here atomically on every change,
        # from session open onward; an unpatched codex (or a pre-open read) leaves it
        # absent, so read_json_dict returns {} and this is None (fall back to the guess).
        return _identity_from_model_state(read_json_dict(codex_model_state_path(self._state_dir)))

    def watched_paths(self) -> tuple[Path, ...]:
        # The single mirror file (watched via its parent dir): every model/effort/tier
        # change rewrites it, so any change wakes the recompute.
        return (codex_model_state_path(self._state_dir),)

    def switch(
        self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]
    ) -> SwitchResult:
        # Codex applies model and effort together via one command -- `/model <model>
        # [effort]` -- so any change to either axis is one send (the patched binary
        # applies it inline; see setup_system.sh). Fast (service_tier=priority) is codex's
        # separate /fast toggle. Only the axes the click changed are sent (see the shared
        # `switch` contract). EAGER_THEN_RECONCILE: the chip moves optimistically on click
        # and reconciles from minds_model_state.json once the command lands.
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
