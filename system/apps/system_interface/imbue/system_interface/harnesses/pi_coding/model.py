"""pi's model catalog and its (EAGER_THEN_RECONCILE) model resolver.

claude and codex declare a handful of models by hand. pi exposes over a thousand
across dozens of providers, each with its own reasoning ("thinking") levels no human
would maintain -- so pi's catalog is PARSED, in the same container, from the same
provider data files pi itself reads. The product is an ordinary
:class:`HarnessCatalog`; nothing downstream can tell a parsed catalog from a
hand-written one, except that pi's uses ``PickerMode.SEARCH`` (the option set is huge
and account-gated, so the picker is a search box, not a list).

The catalog is the master list: it supplies each model's effort levels (verbatim
strings from pi's data) and the label for whatever model an agent is on. Which models
are *offered* to a user is a separate, live concern (``pi --list-models`` for the
authed subset), handled at picker-open time, not here.

The live selection is read by the shared reader
(:func:`~imbue.system_interface.harnesses.model.read_model_identity`) from the uniform
``model_state.json`` the pi lifecycle extension writes at the agent state-dir root,
refreshed at session start (before the first turn), on every ``/model`` or thinking-level
change, and on resume. There is no launch default -- pi is many-provider/many-auth -- so
the bar shows logo-only until the extension records a model. This resolver only owns the
WRITE (switch) side and the auth-gated picker offer set (:meth:`list_offered_models`).
"""

import json
import os
import shutil
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.concurrency_group.subprocess_utils import ProcessSetupError
from imbue.mngr.errors import MngrError
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.file_utils import read_json_dict
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_discovery import start_agent
from imbue.system_interface.harnesses.interrupt import InterruptToComposer
from imbue.system_interface.harnesses.interrupt import PressChord
from imbue.system_interface.harnesses.interrupt import RestartProcess
from imbue.system_interface.harnesses.interrupt import SettleActivity
from imbue.system_interface.harnesses.interrupt import restart_drain
from imbue.system_interface.harnesses.interrupt import try_hold_message_lock
from imbue.system_interface.harnesses.model import HarnessCatalog
from imbue.system_interface.harnesses.model import HarnessModelResolver
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import PickerMode
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.model import SwitchResult
from imbue.system_interface.harnesses.model import to_options
from imbue.system_interface.harnesses.pi_coding.inbox import PI_INBOX_NAME
from imbue.system_interface.harnesses.pi_coding.inbox import PI_INTERRUPT_KEY
from imbue.system_interface.harnesses.pi_coding.inbox import PI_RETRACT_KEY
from imbue.system_interface.harnesses.pi_coding.inbox import append_pi_inbox_sentinel
from imbue.system_interface.harnesses.session import AtomicShoulderTap
from imbue.system_interface.harnesses.session import ShoulderTapOutcome
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.subprocess_runner import run_detached_command

# The single-slot switch mailbox this resolver writes: switch() atomically OVERWRITES
# it with one JSON intent, so a newer pick replaces an unconsumed older one (buffer of
# size 1, last wins). The pi lifecycle extension consumes it (rename, apply, delete) at
# session start and on its 200ms poll, so a switch parked while the agent is stopped
# applies on the next start. Kept in sync with CONTROL_NAME in the extension
# (mngr_pi_coding/resources/mngr_pi_lifecycle.ts).
_CONTROL_NAME: str = "pi_control.json"

# The lifecycle extension writes model_state.json at the agent state-dir root; the
# registry wires this as the harness's model_state_relative_path (the shared reader reads there).
PI_STATE_RELATIVE_PATH: Path = Path(".")

# pi's thinking ladder, in pi's own order (pi-ai's getSupportedThinkingLevels iterates
# this order). This is pi's ordering, not a curated effort set -- which levels a given
# model actually offers comes from that model's own data below, verbatim as strings.
_PI_THINKING_LEVELS: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

# The per-agent pi config dir (== PI_CODING_AGENT_DIR), where the agent's auth lives.
# Kept in sync with _PI_CONFIG_DIR_RELPATH in mngr_pi_coding's plugin.py.
_PI_CONFIG_DIR_RELPATH: str = "plugin/pi_coding"
# How long to wait for `pi --list-models` before falling back to the whole catalog.
_LIST_MODELS_TIMEOUT_SECONDS: float = 15.0


def _parse_list_models(output: str) -> tuple[str, ...]:
    """The ``provider/model`` tags from ``pi --list-models`` table output.

    The output is a whitespace-column table led by a ``provider  model  ...`` header;
    each data row's first two columns are the provider and model. We skip everything up
    to and including the header, then take the first two tokens of each row. When there
    is no header at all (pi prints "No models available. Use /login ..." when unauthed),
    the result is empty -- meaning the user can offer nothing, which is correct.
    """
    tags: list[str] = []
    header_seen = False
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        if not header_seen:
            if parts[0] == "provider" and parts[1] == "model":
                header_seen = True
            continue
        tags.append(f"{parts[0]}/{parts[1]}")
    return tuple(tags)


def _supported_thinking_levels(model: dict[str, Any]) -> tuple[str, ...]:
    """pi's ``getSupportedThinkingLevels``, ported from pi-ai.

    The ``reasoning`` gate comes FIRST and short-circuits: a non-reasoning model
    supports only ``off``, whatever its ``thinkingLevelMap`` says. Past that gate the
    map is a SPARSE OVERRIDE, not the level set: a null mapping disables that level,
    ``xhigh``/``max`` are offered only when explicitly mapped, and every other level is
    on unless nulled.
    """
    if not model.get("reasoning"):
        return ("off",)
    level_map = model.get("thinkingLevelMap") or {}
    supported: list[str] = []
    for level in _PI_THINKING_LEVELS:
        if level in level_map and level_map[level] is None:
            continue
        if level in ("xhigh", "max") and level not in level_map:
            continue
        supported.append(level)
    return tuple(supported)


def find_provider_data_dir(pi_executable: Path) -> Path | None:
    """pi's bundled provider data dir, resolved from the ``pi`` binary itself.

    ``pi`` is a symlink to ``dist/cli.js`` inside the globally installed
    ``@earendil-works/pi-coding-agent``, whose npm prefix differs per image. Walking up
    from the resolved link finds the data dir without hardcoding a prefix or shelling
    out to node.
    """
    current = pi_executable.resolve()
    while current != current.parent:
        candidate = current / "node_modules" / "@earendil-works" / "pi-ai" / "dist" / "providers" / "data"
        if candidate.is_dir():
            return candidate
        current = current.parent
    return None


def build_catalog(data_dir: Path) -> HarnessCatalog:
    """pi's full catalog from ``<pi-ai>/dist/providers/data/*.json``.

    One file per provider, each shaped ``{api_name: {model_id: model}}``. The provider
    id is the filename stem (equal to the ``provider`` field on every model). Each
    model's efforts are its supported thinking levels, verbatim strings.
    """
    entries: list[tuple[str, tuple[str, ...]]] = []
    for path in sorted(data_dir.glob("*.json")):
        provider_id = path.stem
        for models_by_id in read_json_dict(path).values():
            if not isinstance(models_by_id, dict):
                continue
            for model_id, model in models_by_id.items():
                if isinstance(model, dict):
                    entries.append((f"{provider_id}/{model_id}", _supported_thinking_levels(model)))
    return HarnessCatalog(
        options=to_options(tuple(entries)),
        # EAGER_THEN_RECONCILE: the mailbox consume is guaranteed (single-slot,
        # rename-apply-delete) and switch() wakes a stopped agent, so an optimistic
        # chip reconciles promptly; the picker is auth-filtered, so an eager pick
        # that cannot apply is rare.
        switch_mode=SwitchMode.EAGER_THEN_RECONCILE,
        picker_mode=PickerMode.SEARCH,
        powered_by_text="Powered by Pi Coding",
        # pi interrupts natively via the lifecycle extension (interrupt + resubmit).
        native_atomic_shoulder_tap_possible=True,
    )


def get_catalog() -> HarnessCatalog:
    """pi's master catalog, or an empty one when pi's data is absent (invariant: never raise)."""
    executable = shutil.which("pi")
    data_dir = find_provider_data_dir(Path(executable)) if executable else None
    if data_dir is None:
        logger.warning("pi provider data not found; pi's model catalog will be empty")
        return HarnessCatalog(
            options=(),
            switch_mode=SwitchMode.EAGER_THEN_RECONCILE,
            picker_mode=PickerMode.SEARCH,
            powered_by_text="Powered by Pi Coding",
            # pi interrupts natively via the lifecycle extension (interrupt + resubmit).
            native_atomic_shoulder_tap_possible=True,
        )
    return build_catalog(data_dir)


class PiModelResolver(HarnessModelResolver):
    """Applies a pi agent's switch by writing a single-slot control mailbox the extension
    consumes (waking the agent so it consumes promptly), and reports its auth-gated picker
    offer set (the live read is shared)."""

    _state_dir: Path
    _agent_name: str
    _start_agent: Callable[[str], None]

    @classmethod
    def build(cls, agent_info: AgentInfo, start_agent_fn: Callable[[str], None] = start_agent) -> "PiModelResolver":
        self = cls.__new__(cls)
        self._state_dir = agent_info.agent_state_dir
        self._agent_name = agent_info.name
        self._start_agent = start_agent_fn
        return self

    def list_offered_models(self) -> tuple[str, ...] | None:
        # pi's offer set is account-gated and dynamic: exactly the provider/model pairs the
        # user is authenticated for, which `pi --list-models` reports (reading the agent's own
        # auth via PI_CODING_AGENT_DIR). Run per picker-open so a fresh /login shows up. The
        # full catalog stays the master list -- these ids are matched back to it for labels
        # and thinking levels. On any failure, return None (offer the whole catalog) rather
        # than an empty picker.
        executable = shutil.which("pi")
        if executable is None:
            return None
        pi_config_dir = self._state_dir / _PI_CONFIG_DIR_RELPATH
        env = {**os.environ, "PI_CODING_AGENT_DIR": str(pi_config_dir)}
        try:
            finished = run_detached_command(
                [executable, "--list-models"],
                timeout=_LIST_MODELS_TIMEOUT_SECONDS,
                env=env,
                name="pi --list-models",
            )
        except ProcessSetupError as e:
            logger.warning("pi --list-models could not start for {}: {}", pi_config_dir, e)
            return None
        if finished.is_timed_out or finished.returncode != 0:
            logger.warning(
                "pi --list-models failed for {} (timed_out={}, returncode={})",
                pi_config_dir,
                finished.is_timed_out,
                finished.returncode,
            )
            return None
        return _parse_list_models(finished.stdout)

    def switch(self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]) -> SwitchResult:
        # pi's inbox delivers user messages, not slash commands, so a switch cannot go
        # through ``send``. Instead the resolver writes the intent to a single-slot control
        # mailbox the lifecycle extension consumes and applies via pi.setModel /
        # pi.setThinkingLevel. The write atomically OVERWRITES the file, so the newest
        # intent replaces any unconsumed older one (last wins).
        if ModelAxis.MODEL not in axes and ModelAxis.EFFORT not in axes:
            return SwitchResult(ok=True)
        intent = {"model_id": identity.model_id, "thinking_level": identity.effort}
        control_path = self._state_dir / _CONTROL_NAME
        try:
            atomic_write(control_path, json.dumps(intent))
        except OSError as e:
            logger.warning("pi switch: failed to write control file {}: {}", control_path, e)
            return SwitchResult(ok=False, detail="Failed to record the model switch")
        # Wake a stopped agent so the parked intent applies now instead of at some future
        # start -- matching claude/codex, whose send-delivered switches auto-start. Written
        # AFTER the mailbox so the extension's session-start consume sees the intent. The
        # wake is token-free: it boots the TUI process; the extension applies the switch
        # via pi's native setters, and no turn runs. A start failure only parks the intent
        # (it applies on the next start), so the switch itself still succeeded.
        try:
            self._start_agent(self._agent_name)
        except MngrError as e:
            logger.warning("pi switch: could not wake agent {}: {}", self._agent_name, e)
        # EAGER_THEN_RECONCILE: the chip moves optimistically on click and snaps to the
        # state file once the extension applies (200ms poll when running; session start
        # after a wake).
        return SwitchResult(ok=True)


class PiFlushTapStatus(StrEnum):
    """The atomic flush writer's outcome; the server maps it to an HTTP response.

    TAPPED: the flush sentinel was appended (the extension gates on a running turn itself, so
    an idle tap is a harmless no-op there). SEND_IN_FLIGHT: a message send held ``message.lock``
    past the bounded wait, so nothing was written -- surfaced as a benign 200 no-op, not an error
    (the availability flag greys the button while a send is in flight, so a tap that still races
    one just does nothing and the user retaps).
    """

    TAPPED = "tapped"
    SEND_IN_FLIGHT = "send_in_flight"


def flush_pi_queue_atomic(agent_state_dir: Path) -> PiFlushTapStatus:
    """Append the atomic shoulder-tap flush sentinel to ``pi_inbox`` under mngr's ``message.lock``.

    The write is taken under the SAME lock mngr's send holds -- the discipline the retract path
    below already follows -- so the sentinel is ordered AFTER any in-flight send's inbox append:
    the extension injects that just-landed message as a steer before the sentinel interrupts, and
    the flush resubmits it with the rest of the parked queue instead of racing it. Before this
    only the frontend's button-greying guarded the flush-vs-send race. When a send is still in
    flight past the bounded wait (an idle-start send in its turn-confirm), nothing is written and
    the caller surfaces an explicit failure -- the flush is retryable; a silently misordered
    sentinel is not. Raises :class:`OSError` if the inbox write fails.
    """
    with try_hold_message_lock(agent_state_dir) as held:
        if not held:
            return PiFlushTapStatus.SEND_IN_FLIGHT
        append_pi_inbox_sentinel(agent_state_dir / PI_INBOX_NAME, PI_INTERRUPT_KEY)
    return PiFlushTapStatus.TAPPED


def _combine_return_block(queued_block: str, in_flight_block: str) -> str:
    """Concatenate the queued block and the in-flight (Sending) block, in send order.

    Queued messages (parked first) lead; a message still mid-send follows. Either may be
    empty; the result drops the empties so an empty queue or no in-flight send does not
    inject a blank line. Matches the queued block's own newline join so the composer sees
    one uniform block. (A tiny sibling of claude's identically-named helper; kept per-harness
    rather than shared.)
    """
    return "\n".join(part for part in (queued_block, in_flight_block) if part)


class PiInterruptToComposer(InterruptToComposer):
    """pi's stop button: try the native retract under mngr's message lock, else the restart hammer.

    The native path appends the retract sentinel to the inbox and hands the block back with no
    SIGKILL: the lifecycle extension interrupts the running turn and DISCARDS its parked steers
    (the retract sibling of the shipped flush), so there is no mid-tool-call kill, no
    session-resume cost, and no ``reset_activity_state`` patch-up -- the abort's ``agent_end``
    settles the indicator on its own.

    But the native path is only correct when no send is in flight: an unlocked retract can be
    ordered before an in-flight message's inbox append, which would strand that message (it
    starts a fresh turn while its text is gone from the mirror). So the retract is taken under
    the SAME ``message.lock`` mngr's send holds -- captured under the lock, the block includes a
    just-parked message and the sentinel is appended strictly after it, so the extension injects
    it as a steer and the retract discards it. When a send is still in flight past the bounded
    wait (an idle-start send holding the lock through its turn-confirm), the native ordering
    cannot be guaranteed, so we fall back to the base restart-drain: the SIGKILL boundary stops
    the turn unconditionally, and an in-flight message dies with the process -- never runs. The
    common stop (no concurrent send) keeps the gentle native path.
    """

    _agent_info: AgentInfo
    _agent_state_dir: Path
    _inbox_path: Path

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "PiInterruptToComposer":
        self = cls.__new__(cls)
        self._agent_info = agent_info
        self._agent_state_dir = agent_info.agent_state_dir
        self._inbox_path = agent_info.agent_state_dir / PI_INBOX_NAME
        return self

    def drain_to_composer(
        self,
        watcher: AgentSessionWatcher,
        restart_process: RestartProcess,
        settle_activity: SettleActivity,
        press_chord: PressChord,
        get_in_flight_block: Callable[[], str],
    ) -> str:
        with try_hold_message_lock(self._agent_state_dir) as held:
            if not held:
                # A send is in flight past the bounded wait; take the hammer so the turn is
                # definitely stopped and the in-flight message cannot survive to run. The SIGKILL
                # aborts that send before it commits, so its text must ride the returned block
                # rather than being lost (contract Interrupt/A4: return every not-Delivered
                # message). The queued block leads (parked first), the still-in-flight send
                # follows -- send order. This mirrors claude's not-held branch exactly.
                queued_block = restart_drain(self._agent_info, watcher, restart_process, settle_activity)
                return _combine_return_block(queued_block, get_in_flight_block())
            # Held: no send is mid-flight, so any just-parked message is already in the mirror AND
            # the Sending registry is empty (a resolved send cleared its own record) -- so we do
            # NOT fold the in-flight block here. Folding it on the held branch would double-return
            # a message caught in the post-lock-release/pre-commit window (it is in the queued
            # block AND still in the registry) -- the exact double-count claude's held branch
            # deliberately avoids.
            # Capture before writing the sentinel: the sentinel is what clears the queue (both
            # here and, durably, on the watcher's replay), so the block must be read first. pi's
            # ``get_queued_block`` calls ``_refresh`` itself, so the running turn's own initiating
            # message is popped by its drained ``user_message`` -- the block is the still-queued
            # set, and the sentinel appended below is strictly after any in-flight message's line.
            block = watcher.get_queued_block()
            append_pi_inbox_sentinel(self._inbox_path, PI_RETRACT_KEY)
            watcher.clear_queue()
            return block


class PiAtomicShoulderTap(AtomicShoulderTap):
    """pi's native tap: append the flush sentinel to ``pi_inbox`` under the message lock.

    pi has no per-turn id, so there is no ABA target to compute: the lifecycle extension
    gates the interrupt on "a turn is actually running" itself (a no-op when idle). The whole
    locked write (bounded lock acquire, sentinel append) lives with ``flush_pi_queue_atomic``.
    ``SEND_IN_FLIGHT`` is a benign no-op outcome, not an error: the availability flag greys
    the button while a send is in flight, so a tap that still races one just does nothing.
    """

    _agent_info: AgentInfo

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "PiAtomicShoulderTap":
        self = cls.__new__(cls)
        self._agent_info = agent_info
        return self

    def tap(
        self,
        watcher: AgentSessionWatcher,
        press_chord: Callable[[], bool],
        send_recovery: Callable[[str], bool],
    ) -> ShoulderTapOutcome:
        try:
            status = flush_pi_queue_atomic(self._agent_info.agent_state_dir)
        except OSError as e:
            logger.opt(exception=e).error("Failed to write pi interrupt sentinel for {}", self._agent_info.name)
            return ShoulderTapOutcome(
                status="error",
                error_detail=f"Failed to record the shoulder tap for agent '{self._agent_info.name}'",
                error_status_code=500,
            )
        return ShoulderTapOutcome(status=status.value)
