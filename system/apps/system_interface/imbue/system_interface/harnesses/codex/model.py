"""Codex's model catalog and its model resolver.

The patched codex ("codex-in-minds") mirrors its effective model to disk: on
every model/effort change and every service-tier change it atomically writes
``$CODEX_HOME/minds_model_state.json`` -- ``{"model", "effort", "fast"}``, the
uniform live-state schema -- covering framework-initiated changes too (session
configure/resume, server-pushed thread-settings, thread switches, the
out-of-usage "switch model" prompt, fast-mode toggles). The file exists from
session open (holding the launch values) and updates in well under 100ms. The
shared reader (:func:`~imbue.system_interface.harnesses.model.read_model_identity`)
parses that file via the harness's registered relative path
(``plugin/codex/home``, i.e. under CODEX_HOME); this resolver only owns the WRITE
(switch) side.

An installed binary that still emits the older ``{model, reasoning_effort,
service_tier}`` schema is handled gracefully by the shared reader: the model chip
lights (``model`` is unchanged) with effort None / fast off until the next binary
bake. When no patched codex has written the file it is absent, the reader returns
``None``, and the bar shows logo-only.
"""

from collections.abc import Callable
from pathlib import Path

from imbue.mngr_codex.codex_config import CODEX_HOME_RELATIVE_PATH
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

CODEX_CATALOG: HarnessCatalog = HarnessCatalog(
    options=(
        ModelOption(id="gpt-5.6-sol", label="GPT-5.6-Sol", efforts=_CODEX_EFFORTS, supports_fast=True),
        ModelOption(id="gpt-5.6-terra", label="GPT-5.6-Terra", efforts=_CODEX_EFFORTS, supports_fast=True),
        ModelOption(id="gpt-5.6-luna", label="GPT-5.6-Luna", efforts=_CODEX_EFFORTS, supports_fast=True),
        ModelOption(id="gpt-5.5", label="GPT-5.5", efforts=_CODEX_EFFORTS, supports_fast=True),
        ModelOption(id="gpt-5.2", label="GPT-5.2", efforts=_CODEX_EFFORTS, supports_fast=True),
    ),
    # EAGER_THEN_RECONCILE: the patched codex applies /model <model> [effort] inline and
    # mirrors the effective model to minds_model_state.json within ~100ms, which the
    # watcher reconciles into the chip. That write is fast and reliable enough to move the
    # chip optimistically on click and snap it to the pushed live choice a beat later
    # (the frontend's 5-minute pending fallback only fires if the switch never lands).
    switch_mode=SwitchMode.EAGER_THEN_RECONCILE,
    picker_mode=PickerMode.LIST,
    powered_by_label="Codex",
)

# Codex writes its live state under CODEX_HOME (``<state_dir>/plugin/codex/home``), not at
# the state-dir root, so the shared reader/watch path takes this relative directory as data.
CODEX_STATE_RELATIVE_PATH: Path = Path(*CODEX_HOME_RELATIVE_PATH)


class CodexModelResolver(HarnessModelResolver):
    """Switches a codex agent's selection (the live read is shared)."""

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "CodexModelResolver":
        return cls.__new__(cls)

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
