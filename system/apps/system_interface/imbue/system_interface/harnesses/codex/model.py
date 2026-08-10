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
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any
from typing import Protocol

from loguru import logger

from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.mngr_codex.codex_config import CODEX_HOME_RELATIVE_PATH
from imbue.mngr_codex.codex_config import mark_codex_agent_idle
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_discovery import get_host_dir
from imbue.system_interface.harnesses.codex.activity_state import current_open_turn_id
from imbue.system_interface.harnesses.interrupt import InterruptToComposer
from imbue.system_interface.harnesses.interrupt import PressChord
from imbue.system_interface.harnesses.interrupt import RestartProcess
from imbue.system_interface.harnesses.interrupt import SettleActivity
from imbue.system_interface.harnesses.interrupt import restart_drain
from imbue.system_interface.harnesses.interrupt import try_hold_message_lock
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

# How long the stop path waits, after appending the retract control line, for the retracted
# turn's abort to land in the rollout (its ``turn_aborted``/``turn_completed`` boundary) before
# giving up on lifecycle-marker cleanup. Budget owned here, by the harness that knows its
# runtime: the patched binary tails the control file and aborts within well under a second, and
# the watcher reads the live rollout directly, so the boundary is normally observed on the
# first poll or two; the deadline only bounds the version-skew case (an old binary skips the
# retract line), where the turn keeps running and the markers must NOT be cleared.
RETRACT_SETTLE_DEADLINE_SECONDS: float = 5.0
_RETRACT_SETTLE_POLL_INTERVAL_SECONDS: float = 0.2


class CodexQueueWatcher(Protocol):
    """The slice of the session watcher codex's stop and flush paths need. A Protocol so tests
    inject plain fakes; the real :class:`~harnesses.session_watcher.AgentSessionWatcher`
    satisfies it structurally."""

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Read the rollout and return parsed events; the single point that refreshes the mirror."""
        ...

    def get_queued_block(self) -> str:
        """The current parked-queue mirror as one concatenated block (empty == nothing queued)."""
        ...

    def clear_queue(self) -> None:
        """Drop the queued mirror and push the now-empty snapshot."""
        ...


def codex_control_path(agent_state_dir: Path) -> Path:
    """The agent's shoulder-tap control file: the agent state dir, under codex's CODEX_HOME
    relative dir, then the shared control filename -- one path for the flush and retract writers."""
    return agent_state_dir / CODEX_STATE_RELATIVE_PATH / CODEX_SHOULDER_TAP_ATOMIC_CONTROL_NAME


def _append_control_line(control_path: Path, intent: dict[str, str]) -> None:
    """Append one JSON intent line to the shoulder-tap control file, creating dirs as needed."""
    control_path.parent.mkdir(parents=True, exist_ok=True)
    with control_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(intent) + "\n")


class CodexFlushTapStatus(StrEnum):
    """The atomic flush writer's outcome; the server maps it to an HTTP response.

    TAPPED: a control line targeting the live open turn was written. NO_OPEN_TURN: no turn is
    running, so nothing was written (an idempotent no-op). SEND_IN_FLIGHT: a message send held
    ``message.lock`` past the bounded wait, so nothing was written -- the caller reports an
    explicit, retryable failure rather than racing the send.
    """

    TAPPED = "tapped"
    NO_OPEN_TURN = "no_open_turn"
    SEND_IN_FLIGHT = "send_in_flight"


def flush_codex_queue_atomic(agent_state_dir: Path, watcher: CodexQueueWatcher) -> CodexFlushTapStatus:
    """Append the atomic shoulder-tap flush control line under mngr's ``message.lock``.

    The write is taken under the SAME lock mngr's send holds -- the discipline the retract path
    below already follows -- so the control line is ordered AFTER any in-flight send: codex's
    send is STRICT (it holds the lock until the steer has durably parked), which means a flush
    that acquires the lock merges that just-parked steer too, instead of racing it. Before this
    only the frontend's button-greying guarded the flush-vs-send race. The open-turn resolution
    also runs under the lock, so the ABA target is read from a settled rollout. When a send is
    still in flight past the bounded wait (an idle-start send in its turn-confirm), nothing is
    written and the caller surfaces an explicit failure -- the flush is retryable; a silently
    misordered control line is not. Raises :class:`OSError` if the control write fails.
    """
    with try_hold_message_lock(agent_state_dir) as held:
        if not held:
            return CodexFlushTapStatus.SEND_IN_FLIGHT
        target_turn_id = current_open_turn_id(watcher.get_all_events())
        if target_turn_id is None:
            # No turn is running, so there is nothing to merge into -- do NOT write a control
            # line (a stale target_turn_id would gate against a turn that never comes).
            return CodexFlushTapStatus.NO_OPEN_TURN
        _append_control_line(codex_control_path(agent_state_dir), {"target_turn_id": target_turn_id})
    return CodexFlushTapStatus.TAPPED


def _settle_markers_after_retract(
    watcher: CodexQueueWatcher,
    target_turn_id: str,
    *,
    mark_idle: Callable[[], None],
    now: Callable[[], float],
    sleep: Callable[[float], None],
    settle_deadline_seconds: float,
    poll_interval_seconds: float,
) -> None:
    """Watch for the retracted turn's end, then clear the stranded lifecycle markers.

    codex fires no Stop hook when a turn is aborted, so the ``codex_root_active`` flag (and
    with it the ``active`` marker) stays stranded and the lifecycle reports RUNNING forever.
    Mirror of claude's chord path (confirm-before-clear, then ``mark_claude_agent_idle``):
    poll the rollout until the retracted turn is no longer the open one, and only then run
    the mngr_codex idle-marking primitive -- the hooks' own lock + recompute, so in-flight
    subagents keep the marker. Three terminal readings:

    - Boundary observed, no turn open -> mark idle (best-effort: the interrupt already
      succeeded, so a cleanup failure is logged, never surfaced as a stop failure).
    - Boundary observed but a NEW turn is already open (a send landed in the gap) -> clear
      nothing; the new turn's own lifecycle hooks legitimately own the markers now.
    - Deadline with the retracted turn still open (version skew: an old binary skipped the
      retract line) -> clear nothing; the turn is genuinely running and its natural end will
      settle the markers through the Stop hook.
    """
    deadline = now() + settle_deadline_seconds
    open_turn_id = current_open_turn_id(watcher.get_all_events())
    while open_turn_id == target_turn_id and now() < deadline:
        sleep(poll_interval_seconds)
        open_turn_id = current_open_turn_id(watcher.get_all_events())
    if open_turn_id == target_turn_id:
        logger.warning(
            "codex stop: abort of turn {} not observed within {}s; leaving lifecycle markers alone",
            target_turn_id,
            settle_deadline_seconds,
        )
    elif open_turn_id is None:
        try:
            mark_idle()
        except (ConcurrencyGroupError, OSError) as e:
            logger.opt(exception=e).warning("codex stop: abort observed but marking idle failed; indicator will lag")
    else:
        logger.info("codex stop: a new turn opened after the retract; its own lifecycle owns the markers")


def execute_codex_stop_to_composer(
    *,
    agent_state_dir: Path,
    watcher: CodexQueueWatcher,
    mark_idle: Callable[[], None],
    restart_drain_to_base: Callable[[], str],
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    settle_deadline_seconds: float = RETRACT_SETTLE_DEADLINE_SECONDS,
    poll_interval_seconds: float = _RETRACT_SETTLE_POLL_INTERVAL_SECONDS,
) -> str:
    """Interrupt a codex turn via the native retract control line; return the queued block.

    The whole verdict path of :class:`CodexInterruptToComposer` (see its docstring for the
    contract): ``mark_idle`` / ``restart_drain_to_base`` are injected so the override wires
    the real mngr boundaries and tests substitute fakes; ``now`` / ``sleep`` drive the
    bounded post-retract settle watch. Branches:

    - ``message.lock`` still held past the bounded wait -> ``restart_drain_to_base()`` (the
      hammer stops the turn; an in-flight message dies with the process, never runs).
    - No open turn, mirror NONEMPTY -> hand the captured block back and clear the mirror:
      nothing is running, so the parked messages would otherwise sit unreachable as ghost
      chips (and the handback cannot race a live turn). No control line is written.
    - No open turn, mirror empty -> ``""`` (a pure no-op).
    - Open turn -> append ``{"retract_turn_id": ...}`` under the lock, clear the mirror,
      then (outside the lock, so sends are not blocked) watch for the abort and clear the
      stranded lifecycle markers -- see :func:`_settle_markers_after_retract`.
    """
    with try_hold_message_lock(agent_state_dir) as held:
        if not held:
            # A send is in flight past the bounded wait; take the hammer so the turn is
            # definitely stopped and the in-flight message cannot survive to run.
            return restart_drain_to_base()
        # Held: the STRICT send has released, so any just-parked steer is in the sidecar now.
        # Refresh-first: get_all_events drives the watcher's consume of the queued-input
        # sidecar (a bare get_queued_block does not), so the captured block is the parked set.
        events = watcher.get_all_events()
        # Capture before writing the control line: the retract clears the queue, so read first.
        block = watcher.get_queued_block()
        target_turn_id = current_open_turn_id(events)
        if target_turn_id is None:
            # No turn is running, so there is nothing to retract and no control line is
            # written (a stale id would gate against a turn that never comes). But queued
            # messages with no turn to drain into are stranded -- nothing is running to
            # commit them -- so hand them back and clear the mirror; with the lock held the
            # handback cannot race a live turn.
            if block:
                watcher.clear_queue()
                return block
            return ""
        _append_control_line(codex_control_path(agent_state_dir), {"retract_turn_id": target_turn_id})
        watcher.clear_queue()
    # Outside the lock (the settle watch must not block concurrent sends): wait for the abort
    # to land, then clear the markers the abort strands. The block is already captured, so a
    # failure here degrades only the indicator, never the handback.
    _settle_markers_after_retract(
        watcher,
        target_turn_id,
        mark_idle=mark_idle,
        now=now,
        sleep=sleep,
        settle_deadline_seconds=settle_deadline_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    return block


class CodexModelResolver(HarnessModelResolver):
    """Switches a codex agent's selection (the live read is shared)."""

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "CodexModelResolver":
        return cls.__new__(cls)

    def switch(self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]) -> SwitchResult:
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
    """codex's stop button: try the native retract control line under mngr's message lock, else
    the restart hammer.

    The native path is the retract sibling of the atomic shoulder tap: rather than SIGKILL-restart
    the agent and settle activity, append ``{"retract_turn_id": "<id>"}`` to the same
    ``shoulder_tap_atomic.jsonl`` the flush writes. The patched binary interrupts that exact turn
    (ABA-gated on the id) and DISCARDS its parked steers, so there is no mid-tool SIGKILL, no
    session-resume cost, and no ``reset_activity_state`` patch-up: the rollout's ``turn_aborted``
    settles the indicator, and the fork's ``queued_retracted`` records clear the mirror on replay.

    The retract is taken under the SAME ``message.lock`` mngr's send holds, so an in-flight
    message is ordered before the control line. codex's send is STRICT -- it holds the lock until
    the steer has PARKED (the queued-input sidecar record is written synchronously with the park)
    -- so once the lock is acquired the steer is already in ``pending_steers`` and the retract
    discards it (and it is in the captured block, so it reaches the composer). When a send is
    still in flight past the bounded wait, we fall back to the base restart-drain: the SIGKILL
    boundary stops the turn and an in-flight message dies with the process -- never runs. With no
    turn open there is nothing to interrupt and no line is written; queued messages (if any) are
    handed back and the mirror cleared, since nothing is running to commit them.

    Because codex fires no Stop hook on an abort, the retract would strand mngr's
    ``codex_root_active``/``active`` markers (lifecycle RUNNING forever); once the abort is
    observed in the rollout the mngr_codex idle-marking primitive clears them, the way claude's
    chord path runs ``mark_claude_agent_idle`` after its confirmed abort. Delegates the whole
    verdict path to :func:`execute_codex_stop_to_composer`, wiring the real mngr boundaries.
    """

    _agent_info: AgentInfo
    _agent_state_dir: Path

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "CodexInterruptToComposer":
        self = cls.__new__(cls)
        self._agent_info = agent_info
        self._agent_state_dir = agent_info.agent_state_dir
        return self

    def drain_to_composer(
        self,
        watcher: AgentSessionWatcher,
        restart_process: RestartProcess,
        settle_activity: SettleActivity,
        press_chord: PressChord,
    ) -> str:
        return execute_codex_stop_to_composer(
            agent_state_dir=self._agent_state_dir,
            watcher=watcher,
            mark_idle=lambda: mark_codex_agent_idle(self._agent_state_dir, get_host_dir()),
            restart_drain_to_base=lambda: restart_drain(self._agent_info, watcher, restart_process, settle_activity),
        )
