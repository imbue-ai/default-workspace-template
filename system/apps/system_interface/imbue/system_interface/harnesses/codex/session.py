"""Codex's harness session: the one live app-server connection + message ledger.

Codex owns its message lifecycle natively in the daemon, so this session wraps
:class:`CodexLiveConnection` and answers every control-surface question off the ledger:
send, Sending state, tap availability, the tap itself, and the native turn interrupt.
The per-agent dynamic model option set (D2) lives here too -- seeded from the
connect-time ``model/list``, refreshed by each picker-open, persisted to the sidecar
for offline restarts.

Lock discipline: ``_lock`` guards only the connection build/close transitions (held
through the blocking connect, which serializes one agent's rebuilds); every reader takes
a lock-free atomic attribute read instead. That keeps ``is_tap_available`` safe to call
under the manager's own lock without any ordering between the two.
"""

import threading
from collections.abc import Callable

from loguru import logger

from imbue.mngr_codex.app_server_client import CodexAppServerError
from imbue.mngr_codex.app_server_client import TransportClosedError
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_discovery import SendFailedError
from imbue.system_interface.harnesses.codex.ledger import CodexMessageLedger
from imbue.system_interface.harnesses.codex.live_connection import CodexLiveConnection
from imbue.system_interface.harnesses.codex.model import codex_models_to_options
from imbue.system_interface.harnesses.codex.model import get_codex_model_options_path
from imbue.system_interface.harnesses.codex.model import read_codex_model_options
from imbue.system_interface.harnesses.codex.model import write_codex_model_options
from imbue.system_interface.harnesses.interrupt import PressChord
from imbue.system_interface.harnesses.interrupt import RestartProcess
from imbue.system_interface.harnesses.interrupt import SettleActivity
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.session import AgentHarnessSession
from imbue.system_interface.harnesses.session import SendOutcome
from imbue.system_interface.harnesses.session import SessionDeps
from imbue.system_interface.harnesses.session import ShoulderTapOutcome
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher


class CodexHarnessSession(AgentHarnessSession):
    _lock: threading.Lock
    _connection: CodexLiveConnection | None
    _options: tuple[ModelOption, ...] | None
    _is_closed: bool

    @classmethod
    def build(cls, deps: SessionDeps) -> "CodexHarnessSession":
        self = cls.__new__(cls)
        self._deps = deps
        self._lock = threading.Lock()
        self._connection = None
        self._options = None
        self._is_closed = False
        return self

    def _live_ledger(self) -> CodexMessageLedger | None:
        """The ledger behind a currently-alive connection, or ``None`` (daemon down/starting)."""
        connection = self._connection
        if connection is None or not connection.is_alive:
            return None
        return connection.ledger

    # -- liveness ---------------------------------------------------------------------------

    def ensure_live(self) -> None:
        """Ensure a live app-server connection + ledger (idempotent, self-healing).

        A live connection is left alone; one whose daemon generation died is reaped and
        rebuilt, whose fresh ledger starts with an empty queue (the queue is EPHEMERAL --
        nothing from the dead generation is revived). When the daemon is not yet reachable
        (a just-created agent still starting) the build yields no connection and this simply
        retries on the next call. The blocking connect runs under this session's own lock,
        which serializes concurrent builds for THIS agent without touching the manager's.
        """
        with self._lock:
            if self._is_closed or not self._deps.is_tracked():
                return
            existing = self._connection
            if existing is not None and existing.is_alive:
                return
            if existing is not None:
                self._connection = None
                existing.stop()
            connection = CodexLiveConnection.build(
                self._deps.state_dir,
                on_queue_snapshot=self._deps.on_queue_snapshot,
                on_user_turn=self._deps.on_user_turn,
                model_state_path=self._deps.model_state_path,
            )
            if connection is None:
                return
            if self._is_closed:
                # Torn down while we connected: don't leak the fresh connection.
                connection.stop()
                return
            self._connection = connection
            # Seed the ONE reconciled per-agent option set from the connect-time ``model/list``
            # (D2), so the chip can match before any picker-open. A failed connect fetch (empty)
            # is NOT stored -- it must not clobber a set a prior picker-open already populated.
            # The same fresh raw list is written through to the sidecar so the chip still
            # resolves after a restart, before this connection is re-established.
            if connection.codex_models:
                self._options = codex_models_to_options(connection.codex_models)
                write_codex_model_options(get_codex_model_options_path(self._deps.state_dir), connection.codex_models)
        # Outside the lock (it re-enters the manager): a freshly (re)built connection's agent
        # shows the right dot immediately.
        self._deps.recompute_activity()

    def on_lifecycle_dead(self) -> None:
        """Reap the connection and the manager-side queue chips for a positively-dead daemon.

        The one thing the ledger cannot self-observe is its own daemon dying abruptly (no idle
        sweep is emitted), so the lifecycle observer drives this. Level-triggered and
        idempotent; the session stays open, so a restarted daemon rebuilds on the next
        ``ensure_live``.
        """
        with self._lock:
            connection = self._connection
            self._connection = None
        if connection is not None:
            connection.stop()
        self._deps.clear_queue_state()

    def close(self) -> None:
        with self._lock:
            self._is_closed = True
            connection = self._connection
            self._connection = None
        if connection is not None:
            connection.stop()

    # -- messages ---------------------------------------------------------------------------

    def send(self, text: str, message_id: str) -> SendOutcome:
        """Send through the live ledger (contract A2: the ledger is the sole authority).

        ``message_id`` is passed only as a CORRELATION TOKEN (``clientUserMessageId``), which
        codex echoes back on the committed item so the ledger can link the commit to this
        send -- it is NOT the delivery key (Fix 2). OK means the daemon ACCEPTED the message
        (opening a turn, or parking a steer); the ledger then carries its real state, which is
        what drops the frontend's optimistic "Sending" bubble -- so a send the daemon never
        accepted must NOT be OK, or that bubble waits forever on an arrival that cannot come.

        A transport that died under the submit is the same daemon-unreachable condition a
        failed connection build reports, just observed a moment later (the reader thread
        notices a closed transport only on its next poll), so both map to NOT_READY: the
        endpoint's revive-retry loop rebuilds the connection and re-submits with the SAME
        ``message_id``, which every dedup layer keys on, so the retry stays one message. Any
        other daemon refusal raises ``SendFailedError`` with the daemon's own words; the
        endpoint's error response restores the text to the composer.
        """
        self.ensure_live()
        ledger = self._live_ledger()
        if ledger is None:
            return SendOutcome.NOT_READY
        try:
            ledger.send(text, client_id=message_id)
        except TransportClosedError as exc:
            logger.debug("codex session: transport died under a send ({}); reporting not-ready", exc)
            return SendOutcome.NOT_READY
        except CodexAppServerError as exc:
            raise SendFailedError(str(exc)) from exc
        return SendOutcome.OK

    def is_sending(self) -> bool:
        ledger = self._live_ledger()
        return ledger.is_sending() if ledger is not None else False

    def in_flight_block(self) -> str:
        # The ledger returns its own not-yet-committed messages through the interrupt itself
        # (see ``interrupt_to_composer``), so there is no separate in-flight block to fold.
        return ""

    # -- turn control -----------------------------------------------------------------------

    def is_tap_available(self, *, has_queued: bool) -> bool:
        """The ledger's own view: queue non-empty AND nothing Sending -- which also greys the
        button through the interrupt+resend of a tap (the re-sent chips are Sending)."""
        ledger = self._live_ledger()
        if ledger is None:
            return False
        return ledger.is_tap_available()

    def shoulder_tap(
        self,
        agent_info: AgentInfo,
        watcher: AgentSessionWatcher,
        press_chord: Callable[[], bool],
        send_recovery: Callable[[str], bool],
    ) -> ShoulderTapOutcome:
        """Deliver the parked queue EARLY through the live ledger (Fix 3): interrupt the
        running turn and re-send the queue as ONE combined turn, keeping every message
        continuously visible as a Sending chip through the interrupt+resend (contract A1a).
        No live daemon connection means nothing is parked to flush -- a clean no-op."""
        ledger = self._live_ledger()
        if ledger is None:
            return ShoulderTapOutcome(status="no_open_turn")
        result = ledger.shoulder_tap()
        # ``block`` is non-empty only when the combined resend failed to submit: the parked
        # text is handed back to the composer so it is never swallowed (contract A1a).
        return ShoulderTapOutcome(status=result.status, block=result.returned_block)

    def interrupt_to_composer(
        self,
        agent_info: AgentInfo,
        watcher: AgentSessionWatcher,
        restart_process: RestartProcess,
        settle_activity: SettleActivity,
        press_chord: PressChord,
    ) -> str:
        """One native ``turn/interrupt`` on the running turn, then an authoritative per-id
        settle: every non-committed owned message returns to the composer in send order,
        while a message that committed before the interrupt stays Delivered (contract
        Interrupt + A4). The restart/chord capabilities go unused -- the daemon stays up.
        With no live connection there is nothing running and nothing parked. The dot is
        settled via ``settle_activity`` like every other harness's stop: it normally
        clears when the rollout's turn_aborted marker lands, but a daemon dying
        mid-interrupt emits no marker, and without the settle the dot would stay lit
        forever."""
        ledger = self._live_ledger()
        if ledger is None:
            return ""
        block = ledger.interrupt()
        settle_activity()
        return block

    # -- model options ----------------------------------------------------------------------

    def switch_options(self) -> tuple[ModelOption, ...]:
        """The ONE reconciled per-agent option set (D2) -- what the picker offered, the chip
        matches, and the switch endpoint validates against.

        The live in-memory set wins; while it is empty (post-restart, before the daemon
        reconnects) this falls back to the persisted raw ``model/list`` sidecar, mapped on
        read, so the chip resolves offline instead of showing the unrecognized-model shrug.
        Empty only when neither source is populated -- a switch then fails validation, which
        is correct: nothing to switch to until a connect or picker-open supplies the set.
        """
        cached = self._options
        if cached is not None:
            return cached
        models = read_codex_model_options(get_codex_model_options_path(self._deps.state_dir))
        if not models:
            return ()
        return codex_models_to_options(models)

    def note_offered_options(self, options: tuple[ModelOption, ...]) -> None:
        """Record a fresh picker-open fetch (D2 reconciliation point). Callers do not store a
        failed (empty) fetch, so a transient daemon miss never clobbers the last-known set."""
        self._options = options
