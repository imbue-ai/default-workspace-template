"""antigravity's session: the hold-the-queue send, plus the unknown-model rendering fallback.

Two reasons this subclass exists.

**Sending.** agy accepts a message mid-turn only by parking it invisibly inside its TUI,
where nothing can observe it and from where agy merges it into one turn. So we never type
into a busy agy: while a turn is open the message is HELD here and delivered by the watcher's
flush worker once agy goes idle. See :meth:`send` and
docs/design/antigravity-message-lifecycle-plan.md.

**The model chip.** See :meth:`switch_options`.
"""

import threading
from datetime import datetime
from datetime import timezone

from loguru import logger

from imbue.system_interface.activity_state import ACTIVE_MARKER_FILENAME
from imbue.system_interface.harnesses.antigravity.model import derived_option
from imbue.system_interface.harnesses.antigravity.queue_tracker import AntigravityQueueTracker
from imbue.system_interface.harnesses.antigravity.queue_tracker import OUTBOX_FILENAME
from imbue.system_interface.harnesses.antigravity.queue_tracker import get_tracker
from imbue.system_interface.harnesses.antigravity.queue_tracker import session_token
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.model import match_option
from imbue.system_interface.harnesses.model import read_model_identity
from imbue.system_interface.harnesses.sending_registry import SendingRegistry
from imbue.system_interface.harnesses.interrupt import try_hold_message_lock
from imbue.system_interface.harnesses.session import FileHarnessSession
from imbue.system_interface.harnesses.session import SendOutcome
from imbue.system_interface.harnesses.session import SessionDeps


class AntigravityHarnessSession(FileHarnessSession):
    """agy's file session, with a derived option appended for a model the catalog lacks."""

    @classmethod
    def build(cls, deps: SessionDeps) -> "AntigravityHarnessSession":
        # Declared so the subclass is the STATIC type too; the base already constructs via
        # ``cls``, so this only narrows the annotation (same as CodexHarnessSession).
        self = cls.__new__(cls)
        self._deps = deps
        self._sending = SendingRegistry.build()
        self._sending_lock = threading.Lock()
        return self

    def send(self, text: str, message_id: str) -> SendOutcome:
        """Type into agy only when it is idle; otherwise hold the message for the flush.

        The decision is made **while holding agy's message lock** -- the same lock mngr takes
        for the whole of its own send. That is what makes two rapid sends safe: agy's send does
        not return until its busy marker advances, so a second send that acquires the lock
        necessarily observes the first one's effect. Deciding before taking the lock would let
        us read "idle" while a previous send is still in flight, type into a busy agy, and lose
        the message into its invisible parked block -- the exact failure this design exists to
        prevent.

        Failing to take the lock means a send IS in flight, so the agent is busy by definition
        and the message is held. The wait is ZERO on purpose, unlike stop's: stop waits out an
        in-flight send because it may be about to finish and change what stop must return,
        whereas here the answer is already known the instant the lock is contended. Taking the
        helper's 2s default would stall every send behind the previous one -- precisely the
        rapid-fire case this path exists to serve -- to learn something we already know.

        Residual window, accepted: between releasing the lock and mngr re-taking it for the
        real send, another send can start a turn, so we can type into an agy that just became
        busy. The message is NOT lost when that happens -- agy parks it and delivers it at the
        end of that turn -- it is simply shown as sent rather than queued, arriving later than
        the UI implied. Closing it would mean holding the lock across the delegation, which is
        the deadlock described below.
        """
        with try_hold_message_lock(self._deps.state_dir, wait_seconds=0.0) as is_lock_held:
            is_busy = (not is_lock_held) or (self._deps.state_dir / ACTIVE_MARKER_FILENAME).exists()
        # The delegation MUST happen after the lock is released. mngr's own send takes this
        # same message.lock, and flock is per open-file-description, so a second exclusive
        # acquire from this process blocks forever -- delegating while still holding it
        # deadlocks every idle send.
        if not is_busy:
            return super().send(text, message_id)
        queued_id = self._queue().enqueue(text, _now_iso())
        self._deps.on_queue_snapshot(self._queue().snapshot())
        self._deps.notify_agents_changed()
        logger.debug("antigravity: holding a message for the next flush ({})", queued_id)
        return SendOutcome.OK

    def switch_queue_snapshot(self) -> list[dict[str, Any]]:
        """The held queue, for tests and diagnostics (the live wire copy goes through the
        watcher's snapshot callback)."""
        return self._queue().snapshot()

    def _queue(self) -> AntigravityQueueTracker:
        """The agent's tracker -- the same instance the watcher reads (see queue_tracker).

        Keyed by the state dir's own name, which IS the agent id (mngr lays agents out as
        ``<host_dir>/agents/<agent_id>``); SessionDeps carries no id of its own.
        """
        return get_tracker(
            self._deps.state_dir.name,
            self._deps.state_dir / OUTBOX_FILENAME,
            session_token(self._deps.state_dir),
        )

    def switch_options(self) -> tuple[ModelOption, ...]:
        """The static catalog, plus a derived option when the LIVE model is not in it.

        This is where the catalog's staleness is absorbed. ``match_option`` resolves the
        reported id against this set, and an id it cannot find renders as the unrecognized
        shrug -- which is what a user sees for EVERY agy agent the moment Google ships a
        model newer than the hand-written list, including (worst case) a new default. Adding
        the derived option keeps the chip readable until the list is updated.

        Cheap enough for the recompute path: one small JSON read, the same file
        ``_recompute_model_choice`` has already read to get the identity it is matching.
        """
        options = self._deps.catalog_options()
        identity = read_model_identity(self._deps.model_state_path)
        if identity is None or match_option(identity, options) is not None:
            return options
        return (*options, derived_option(identity.model_id))


def _now_iso() -> str:
    """The enqueue timestamp, in the same ISO/Z shape the other harnesses' entries carry."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
