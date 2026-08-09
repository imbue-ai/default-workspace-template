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

import json
from collections.abc import Callable
from pathlib import Path

from imbue.mngr_codex.codex_config import CODEX_HOME_RELATIVE_PATH
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.codex.activity_state import current_open_turn_id
from imbue.system_interface.harnesses.interrupt import InterruptToComposer
from imbue.system_interface.harnesses.interrupt import PressChord
from imbue.system_interface.harnesses.interrupt import RestartProcess
from imbue.system_interface.harnesses.interrupt import SettleActivity
from imbue.system_interface.harnesses.model import EffortChoice
from imbue.system_interface.harnesses.model import HarnessCatalog
from imbue.system_interface.harnesses.model import HarnessModelResolver
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.model import PickerMode
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.model import SwitchResult
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher

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
    # Codex's patched binary watches shoulder_tap_atomic.jsonl and merges parked steer
    # messages into the live turn (ABA-gated on the turn id), so the "Shoulder tap" button
    # can flush atomically without a restart. Only codex supports this today.
    native_atomic_shoulder_tap_possible=True,
)

# Codex writes its live state under CODEX_HOME (``<state_dir>/plugin/codex/home``), not at
# the state-dir root, so the shared reader/watch path takes this relative directory as data.
CODEX_STATE_RELATIVE_PATH: Path = Path(*CODEX_HOME_RELATIVE_PATH)

# The append-only control file the patched codex binary watches under CODEX_HOME. Each line is
# one JSON intent, ABA-gated on the live turn id so it lands in the exact turn the user acted on
# and no other: a flush ``{"target_turn_id": "<id>"}`` (atomic shoulder tap merges the parked
# steers into that turn) or a retract ``{"retract_turn_id": "<id>"}`` (stop button interrupts
# that turn and discards the parked steers). One filename shared by both writers -- the shoulder
# tap endpoint and the stop-button override below -- so a distinct key, not a distinct file,
# distinguishes the intents (an old binary skips the unknown retract key as malformed, fail-safe).
CODEX_SHOULDER_TAP_ATOMIC_CONTROL_NAME: str = "shoulder_tap_atomic.jsonl"


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


class CodexInterruptToComposer(InterruptToComposer):
    """codex's stop button: interrupt the live turn via a retract control line, hand the block
    back, no restart.

    The retract sibling of the atomic shoulder tap: rather than SIGKILL-restart the agent and
    settle activity, append ``{"retract_turn_id": "<id>"}`` to the same
    ``shoulder_tap_atomic.jsonl`` the flush writes. The patched binary interrupts that exact turn
    (ABA-gated on the id) and DISCARDS its parked steers -- the retract counterpart of the shipped
    flush -- so there is no mid-tool SIGKILL, no session-resume cost, and no ``reset_activity_state``
    patch-up: the rollout's ``turn_aborted`` settles the indicator, and the fork's
    ``queued_retracted`` records clear the mirror on the watcher's replay. With no turn open there
    is nothing to interrupt -- any parked steers are committing on their own -- so the block comes
    back empty and no control line is written. The base restart-drain's ``restart_process`` /
    ``settle_activity`` are unused.
    """

    _control_path: Path

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "CodexInterruptToComposer":
        self = cls.__new__(cls)
        # Same path the atomic shoulder-tap endpoint writes: the agent state dir, under codex's
        # CODEX_HOME relative dir, then the shared control filename.
        self._control_path = (
            agent_info.agent_state_dir / CODEX_STATE_RELATIVE_PATH / CODEX_SHOULDER_TAP_ATOMIC_CONTROL_NAME
        )
        return self

    def drain_to_composer(
        self,
        watcher: AgentSessionWatcher,
        restart_process: RestartProcess,
        settle_activity: SettleActivity,
        press_chord: PressChord,
    ) -> str:
        # Refresh-first: get_all_events drives the watcher's consume of the queued-input sidecar
        # (a bare get_queued_block does not), so the captured block is the currently-parked set.
        events = watcher.get_all_events()
        # Capture before writing the control line: the retract is what clears the queue, so the
        # block must be read first.
        block = watcher.get_queued_block()
        target_turn_id = current_open_turn_id(events)
        if target_turn_id is None:
            # No turn is running, so there is nothing to retract -- do NOT write a control line
            # (a stale id would gate against a turn that never comes) and hand back an empty block.
            return ""
        self._control_path.parent.mkdir(parents=True, exist_ok=True)
        with self._control_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"retract_turn_id": target_turn_id}) + "\n")
        watcher.clear_queue()
        return block
