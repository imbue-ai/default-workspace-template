"""antigravity's session: the hold-the-queue send, plus the unknown-model rendering fallback.

Two reasons this subclass exists.

**Sending.** agy accepts a message mid-turn only by parking it invisibly inside its TUI,
where nothing can observe it and from where agy merges it into one turn. So this session
NEVER types: every message is enqueued, and the watcher's flush worker -- the single typist --
delivers it when no turn is open. See :meth:`send` and
``system/apps/system_interface/imbue/system_interface/harnesses/core-contracts/messages-lifecycle-contract-state-of-things.md`` (E12).

**The model chip.** See :meth:`switch_options`.
"""

import threading
from datetime import datetime
from datetime import timezone

from loguru import logger

from imbue.system_interface.harnesses.antigravity.model import derived_option
from imbue.system_interface.harnesses.antigravity.queue_tracker import AntigravityQueueTracker
from imbue.system_interface.harnesses.antigravity.queue_tracker import OUTBOX_FILENAME
from imbue.system_interface.harnesses.antigravity.queue_tracker import get_tracker
from imbue.system_interface.harnesses.antigravity.queue_tracker import session_token
from imbue.system_interface.harnesses.antigravity.turn_state import get_turn_state
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.model import match_option
from imbue.system_interface.harnesses.model import read_model_identity
from imbue.system_interface.harnesses.sending_registry import SendingRegistry
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
        """Hold the message. Always. This session never types into agy.

        Deciding here whether to type meant reading agy's state, releasing the lock (mngr
        re-acquires the same flock, and flock is per open-file-description, so holding it
        across the delegation deadlocks the process against itself), and only then typing --
        a check-then-act whose window was wide enough to land a message in a turn that had
        just started. agy parks such a message invisibly and merges it into the running turn.

        Enqueueing unconditionally removes the decision, and with it the window. One typist
        exists -- the flush worker -- so there is nothing left to race. The chip is published
        inside the tracker's lock before this returns, so the message is on screen from the
        instant it leaves the composer (contract A1a), and the worker is woken in the same
        breath, so an idle agy is typed into immediately.

        Cost, accepted deliberately: a message to an idle agy renders as a queued chip for one
        worker cycle instead of going straight to a turn. Queued is a real backend-reported
        state, so A2/A3b permit it, and it is strictly better A1a than the previous behaviour,
        where nothing represented the message until its turn committed.
        """
        # How it is PRESENTED depends on whether anything is actually in its way. With one
        # typist, a message accepted against an idle agy is not parked -- the worker was woken
        # inside the enqueue and is about to type it -- so it reads "Sending...", not as a
        # queued chip. Reporting Queued there tells the user a message is waiting when it is
        # already on its way, and flashes the shoulder-tap button on every ordinary send.
        is_turn_open = get_turn_state(self._deps.state_dir.name).is_hold_required(self._deps.state_dir)
        self._queue().enqueue(text, _now_iso(), is_turn_open=is_turn_open)
        self._deps.notify_agents_changed()
        logger.debug("antigravity: holding a message for the flush worker ({})", message_id)
        return SendOutcome.OK

    def is_sending(self) -> bool:
        """Whether anything is in flight, so the shoulder-tap must be withheld.

        With one typist this is exactly "a flush has entries claimed". The base's
        ``SendingRegistry`` is still consulted, because this class no longer records into it
        but the base's own send path would -- keeping both means the gate cannot silently
        regress if a direct send is ever reintroduced.
        """
        return self._queue().is_sending() or super().is_sending()

    # ``in_flight_block`` is deliberately NOT overridden. Claimed entries stay in the queue --
    # that is what keeps them on screen -- so stop already accounts for them there, and
    # returning them here too would put every flushing message into the composer twice.

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
