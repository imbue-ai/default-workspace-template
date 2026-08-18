"""One agent's live harness CONTROL surface -- the write/act half of the harness split.

The read half is :class:`~imbue.system_interface.harnesses.session_watcher.AgentSessionWatcher`
(tails the transcript into the common event schema). This class owns the actions: sending a
message (and the in-flight *Sending* records, contract A1), turn control (interrupt-to-composer
and the atomic shoulder tap), daemon liveness, and the per-agent model option set.
``HarnessSpec.session_class`` names each harness's implementation; ``AgentManager`` builds one
per tracked agent and the server endpoints dispatch through it, so neither names a harness.

Two implementations cover the three harnesses:

- :class:`FileHarnessSession` (claude, pi): the send goes through mngr's locked message API and
  the *Sending* registry lives here -- the record the interrupt paths fold into a stop's
  returned block when a send is caught mid-flight. Interrupt and tap dispatch to the harness's
  registered ``interrupt_to_composer_class`` / ``shoulder_tap_class`` (bound via
  :class:`SessionDeps`, which carries every capability as a callable so this module never
  imports the registry).
- :class:`CodexHarnessSession` (its own module, ``codex/session.py``): wraps the one live
  app-server connection + message ledger, which owns the message lifecycle natively.

Like the watchers, sessions are plain mutable live-state holders constructed via ``build``,
not pydantic models.
"""

import threading
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.interrupt import InterruptToComposer
from imbue.system_interface.harnesses.interrupt import PressChord
from imbue.system_interface.harnesses.interrupt import RestartProcess
from imbue.system_interface.harnesses.interrupt import SettleActivity
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.sending_registry import SendingRegistry
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher


class SendOutcome(StrEnum):
    """How a session's ``send`` resolved; the endpoint maps these to HTTP statuses."""

    OK = "ok"
    # The harness cannot accept messages yet (its daemon is still starting); retryable -> 503.
    NOT_READY = "not_ready"
    # Delivery was attempted and failed -> 500.
    FAILED = "failed"


class ShoulderTapOutcome(FrozenModel):
    """The uniform result of a session's atomic shoulder tap.

    ``status`` carries the harness's tap verdict verbatim (``tapped`` / ``no_open_turn`` /
    ``send_in_flight`` / ...); ``block`` is a returned-to-composer handback (codex resend
    failure). ``error_detail`` set means the endpoint should answer ``error_status_code``
    with that detail instead of a success body.
    """

    status: str
    block: str = ""
    error_detail: str | None = None
    error_status_code: int = 500


class AtomicShoulderTap(ABC):
    """A file harness's native atomic shoulder tap, the tap peer of ``InterruptToComposer``.

    ``build`` takes the whole :class:`AgentInfo` (like the watcher/resolver/interrupter) so
    each harness reads only the paths it needs. Registered per harness on
    ``HarnessSpec.shoulder_tap_class``; a harness that taps through a live connection instead
    (codex) registers none and overrides ``shoulder_tap`` on its session directly.
    """

    @classmethod
    @abstractmethod
    def build(cls, agent_info: AgentInfo) -> "AtomicShoulderTap":
        """Construct for one agent, not yet touching anything."""

    @abstractmethod
    def tap(
        self,
        watcher: AgentSessionWatcher,
        press_chord: Callable[[], bool],
        send_recovery: Callable[[str], bool],
    ) -> ShoulderTapOutcome:
        """Deliver the tap. ``press_chord`` / ``send_recovery`` route through mngr's locked
        message API (the manager binds them); a harness that needs neither ignores them."""


class SessionDeps(FrozenModel):
    """Every capability a session may need, bound once by ``AgentManager``.

    All cross-module lookups (registry dispatch, catalog options, the model-state path) arrive
    here as bound callables/values, so ``session.py`` never imports the registry -- the same
    direction of dependency as ``interrupt.py``. A file session uses the send/notify/dispatch
    half; the codex session uses the connection-callback half; each ignores the rest.
    """

    model_config = {"arbitrary_types_allowed": True}

    harness: HarnessType
    state_dir: Path
    # Deliver one message through mngr's locked message API (blocking); True = delivered/queued.
    send_to_harness: Callable[[str], bool]
    # Push a fresh agents snapshot to clients (tap-button greying rides this).
    notify_agents_changed: Callable[[], None]
    # Whether the manager still tracks this agent (a connection built after teardown is dropped).
    is_tracked: Callable[[], bool]
    # Codex connection callbacks: the ledger's queue snapshot / committed user-turn fan-out.
    on_queue_snapshot: Callable[[list[dict[str, Any]]], None]
    on_user_turn: Callable[[dict[str, Any]], None]
    # Recompute + broadcast this agent's activity dot (after a connection (re)build).
    recompute_activity: Callable[[], None]
    # Drop the manager-side cached queue chips (a dead daemon's ephemeral queue died with it).
    clear_queue_state: Callable[[], None]
    # The harness's static catalog options (the switch-validation set for file harnesses).
    catalog_options: Callable[[], tuple[ModelOption, ...]]
    # Registry dispatch, pre-bound: the harness's interrupter / tap implementations.
    build_interrupter: Callable[[AgentInfo], InterruptToComposer]
    build_shoulder_tap: Callable[[AgentInfo], "AtomicShoulderTap | None"]
    # Where the harness mirrors its uniform ``model_state.json``.
    model_state_path: Path


class AgentHarnessSession(ABC):
    """One agent's live control surface. Concrete no-op defaults mirror the watcher ABC:
    a method a harness has no behavior for needs no override."""

    _deps: SessionDeps

    @classmethod
    @abstractmethod
    def build(cls, deps: SessionDeps) -> "AgentHarnessSession":
        """Construct for one agent; must not block (liveness is ``ensure_live``'s job)."""

    @property
    def harness(self) -> HarnessType:
        """The harness this session was built for (the manager heals a mismatched cache)."""
        return self._deps.harness

    # -- liveness ---------------------------------------------------------------------------

    def ensure_live(self) -> None:
        """Bring up whatever live backend the harness needs (blocking OK). No-op default:
        a file harness has no daemon to connect."""

    def on_lifecycle_dead(self) -> None:
        """The agent's mngr lifecycle is positively dead; drop live state that died with it.
        Level-triggered: called on every recompute observing a dead lifecycle, and must stay
        idempotent. No-op default."""

    def close(self) -> None:
        """Terminal teardown (the manager stopped tracking the agent). No-op default."""

    # -- messages ---------------------------------------------------------------------------

    @abstractmethod
    def send(self, text: str, message_id: str) -> SendOutcome:
        """Deliver one message, owning its whole Sending lifecycle (contract A1)."""

    def is_sending(self) -> bool:
        """Whether any send is currently in flight (greys the shoulder-tap button)."""
        return False

    def in_flight_block(self) -> str:
        """The still-in-flight (Sending) messages as one concatenated block ('' = none)."""
        return ""

    # -- turn control -----------------------------------------------------------------------

    def is_tap_available(self, *, has_queued: bool) -> bool:
        """Whether the shoulder-tap button is offered (contract Shoulder-tap): something is
        queued and nothing is Sending. A live-connection harness overrides with its own view."""
        return has_queued and not self.is_sending()

    @abstractmethod
    def shoulder_tap(
        self,
        agent_info: AgentInfo,
        watcher: AgentSessionWatcher,
        press_chord: Callable[[], bool],
        send_recovery: Callable[[str], bool],
    ) -> ShoulderTapOutcome:
        """Merge the queue into the live turn without restarting the agent."""

    @abstractmethod
    def interrupt_to_composer(
        self,
        agent_info: AgentInfo,
        watcher: AgentSessionWatcher,
        restart_process: RestartProcess,
        settle_activity: SettleActivity,
        press_chord: PressChord,
    ) -> str:
        """Interrupt the running turn and return the queued block to the composer."""

    # -- model options ----------------------------------------------------------------------

    def switch_options(self) -> tuple[ModelOption, ...]:
        """The option set the switch endpoint validates against; static catalog by default."""
        return self._deps.catalog_options()

    def note_offered_options(self, options: tuple[ModelOption, ...]) -> None:
        """A picker-open fetch produced ``options``; a harness with a per-agent dynamic set
        records them (codex, D2 reconciliation). No-op default."""


class FileHarnessSession(AgentHarnessSession):
    """The session for harnesses driven through mngr's file/message APIs (claude, pi).

    Owns the *Sending* registry (contract A1) -- session state, NOT watcher state: the
    watcher is a pure transcript reader. The registry has its own leaf lock (never held
    across an external call), so ``is_sending`` / ``is_tap_available`` are safe to call
    under the manager's lock.
    """

    _sending: SendingRegistry
    _sending_lock: threading.Lock

    @classmethod
    def build(cls, deps: SessionDeps) -> "FileHarnessSession":
        self = cls.__new__(cls)
        self._deps = deps
        self._sending = SendingRegistry.build()
        self._sending_lock = threading.Lock()
        return self

    def send(self, text: str, message_id: str) -> SendOutcome:
        # Record as Sending BEFORE delivery (contract A1): while the synchronous send is in
        # flight the message has no on-disk harness record yet, so a concurrent interrupt reads
        # this to return a not-yet-committed send to the composer rather than lose it (A4).
        # Keyed by the sender's stable send-time id (or a minted one for legacy callers).
        token = message_id or uuid4().hex
        with self._sending_lock:
            self._sending.record(token, text)
        # The agents snapshot now reports the tap unavailable (something is Sending); push it so
        # the button greys for the blocking send's duration, and push again after resolution so
        # it never stays stuck greyed.
        try:
            self._deps.notify_agents_changed()
            success = self._deps.send_to_harness(text)
            return SendOutcome.OK if success else SendOutcome.FAILED
        finally:
            # Resolved on EVERY exit -- delivered/queued (a real representation exists), failed
            # (Returned, not Sending), or an exception from the send (the request 500s and the
            # composer keeps the draft). A leaked record would grey the tap for the session's
            # lifetime and re-inject the same text into every later stop's returned block.
            with self._sending_lock:
                self._sending.resolve(token)
            self._deps.notify_agents_changed()

    def is_sending(self) -> bool:
        with self._sending_lock:
            return bool(self._sending.pending)

    def in_flight_block(self) -> str:
        with self._sending_lock:
            return self._sending.concatenated_block()

    def shoulder_tap(
        self,
        agent_info: AgentInfo,
        watcher: AgentSessionWatcher,
        press_chord: Callable[[], bool],
        send_recovery: Callable[[str], bool],
    ) -> ShoulderTapOutcome:
        tap = self._deps.build_shoulder_tap(agent_info)
        if tap is None:
            # Unreachable behind the endpoint's catalog capability gate; answer 400 anyway
            # rather than crash if a catalog and the spec ever disagree.
            return ShoulderTapOutcome(
                status="unsupported",
                error_detail=(
                    f"Agent '{agent_info.name}' runs the {self._deps.harness.value} harness, which does "
                    "not support an atomic shoulder tap"
                ),
                error_status_code=400,
            )
        return tap.tap(watcher, press_chord, send_recovery)

    def interrupt_to_composer(
        self,
        agent_info: AgentInfo,
        watcher: AgentSessionWatcher,
        restart_process: RestartProcess,
        settle_activity: SettleActivity,
        press_chord: PressChord,
    ) -> str:
        interrupter = self._deps.build_interrupter(agent_info)
        return interrupter.drain_to_composer(
            watcher,
            restart_process,
            settle_activity,
            press_chord,
            self.in_flight_block,
        )
