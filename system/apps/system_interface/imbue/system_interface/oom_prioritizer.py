"""Re-tag chat agents' memory-shedding priority from live workspace activity.

The launch wrapper tags every agent's ``oom_score_adj`` once at startup (see
``system/services/oom_priority``). This engine keeps *chat* agents' scores current: a chat
the user is engaged with is protected from an out-of-memory shed, and a chat left
untouched long enough climbs past the worker band so a stale chat is shed before
the worker a live chat just spawned.

Signals, and where each comes from:

- **open** / **visible** -- the chat page's presence in each client, reported by
  the page itself through the chat app's presence route and aggregated by the
  ``PresenceTracker`` (open while any client's report is unexpired, visible
  while any client's last report says so),
- **messaged** -- a message sent through the chat app's send route; drives a
  recency ranking across all chats, newest-first,
- **running** -- the chat's mngr lifecycle state, pushed in from the observe
  stream via ``record_running_agents``. Entering a running state counts as
  engagement (it is the only evidence of a message sent outside the UI -- by
  ``mngr message`` or by another agent), and staying in one marks the chat
  mid-turn, which suspends its staleness climb until the turn ends.

Idle time is measured against the most recent of those events, wall-clock, with
the agent's own process-start time as a floor so a freshly revived chat counts as
fresh. Across a system-interface restart the message stamps are re-seeded from
the durable client-activity log (``seed_last_message_times``), so a restart does
not hand every chat a fresh grace period.

Only chat agents are managed. Workers and the primary (services) agent are
excluded by the caller's ``list_chat_agent_ids`` (they keep their launch bands --
workers stay maximally expendable, the primary stays pinned), so opening,
switching to, or messaging one of them never moves its score. A chat with no live
process (dormant, revives on its next message) is simply skipped until its
process exists.

Re-tagging is event-driven plus a slow sweep. The events (presence reports,
sends, and lifecycle changes) cover everything that *raises* a chat's
protection; the sweep exists because staleness is the one signal that changes
with no event to announce it -- a chat crosses a ramp threshold simply by sitting
there. The messaged-revive path is race-free without the sweep: the send blocks
until the revived process is ready (and the launch wrapper registers its pid
before that), and the send route records the message only after the send
returns, so ``reapply``'s pid lookup finds the live process.

The band arithmetic lives in ``oom_priority.bands`` (the stdlib-only, testable
policy); this engine only holds the activity state and drives the writes. All
collaborators are injected so the engine is unit-testable without ``/proc``, the
agent manager, or the pid registry.
"""

import threading
import time
from collections.abc import Callable
from collections.abc import Iterable
from typing import Final

from oom_priority import bands

from imbue.system_interface.presence import PresenceState
from imbue.system_interface.presence import PresenceTracker

# How often the sweep re-evaluates staleness. The ramp is measured in hours, so
# minute-granularity is ample; each pass is a handful of stats and ``/proc``
# writes per chat.
SWEEP_INTERVAL_SECONDS: Final[float] = 60.0


class ChatOomPrioritizer:
    """Holds chat activity state and re-tags each chat's ``oom_score_adj``.

    ``list_chat_agent_ids`` returns the ids of the agents to manage (chats only;
    the caller excludes workers and the primary). ``resolve_pid`` maps a chat's
    agent id to its live main-process pid, or None when it has no running process.
    ``set_adj`` writes ``oom_score_adj`` for a pid (best-effort; its return value
    is ignored). ``resolve_process_started_at`` returns the epoch time at which a
    chat's claude process last started, or None when unknown; it floors the
    engagement clock so a revived chat is never treated as stale. ``clock``
    supplies wall-clock epoch seconds -- absolute, not monotonic, because idle
    time is compared against filesystem mtimes and seeded log timestamps.
    ``presence`` holds the per-client presence reports the open and visible
    signals are read from; one on the same clock is built when none is given.
    """

    def __init__(
        self,
        *,
        list_chat_agent_ids: Callable[[], Iterable[str]],
        resolve_pid: Callable[[str], int | None],
        set_adj: Callable[[int, int], bool],
        resolve_process_started_at: Callable[[str], float | None],
        clock: Callable[[], float] = time.time,
        sweep_interval_seconds: float = SWEEP_INTERVAL_SECONDS,
        presence: PresenceTracker | None = None,
    ) -> None:
        self._list_chat_agent_ids = list_chat_agent_ids
        self._resolve_pid = resolve_pid
        self._set_adj = set_adj
        self._resolve_process_started_at = resolve_process_started_at
        self._clock = clock
        self._sweep_interval_seconds = sweep_interval_seconds
        self._lock = threading.Lock()
        self._presence = presence if presence is not None else PresenceTracker(clock=clock)
        # agent_id -> time of its most recent message, for recency ranking.
        self._last_message_at: dict[str, float] = {}
        # agent_id -> time of its most recent engagement of *any* kind (messaged,
        # switched to, or entered a running state), for the staleness clock.
        self._last_engaged_at: dict[str, float] = {}
        # Agents currently in a running lifecycle state, i.e. mid-turn.
        self._running: set[str] = set()
        self._sweep_stop = threading.Event()
        self._sweep_thread: threading.Thread | None = None

    def start(self) -> None:
        """Begin the staleness sweep and apply the current state once.

        Separate from construction so tests (and any caller that only wants the
        event-driven behaviour) can drive ``record_*``/``reapply`` directly
        without a background thread.
        """
        self.reapply()
        self._sweep_stop.clear()
        thread = threading.Thread(target=self._run_sweep, daemon=True, name="oom-chat-sweep")
        self._sweep_thread = thread
        thread.start()

    def stop(self) -> None:
        """Stop the staleness sweep. Idempotent; safe if ``start`` never ran."""
        self._sweep_stop.set()
        thread = self._sweep_thread
        if thread is not None:
            thread.join(timeout=5)
            self._sweep_thread = None

    def seed_last_message_times(self, last_message_at_by_agent_id: dict[str, float]) -> None:
        """Seed per-chat last-message times from a durable record, at startup.

        Without this, a system-interface restart would leave every chat with no
        message history and so no recency ranking, and would reset the staleness
        clock to each chat's process-start time -- handing a chat that has been up
        and untouched for days a fresh grace period. Only ever moves a stamp
        forward, so seeding cannot un-engage a chat that was already reported.
        """
        with self._lock:
            for agent_id, messaged_at in last_message_at_by_agent_id.items():
                self._stamp_message_locked(agent_id, messaged_at)

    def record_presence(self, agent_id: str, client_id: str, state: PresenceState) -> None:
        """Apply one client's presence report about one chat, then re-tag every chat.

        The report replaces that client's standing one (idempotent and self-healing:
        the page's heartbeat corrects any missed one). Non-chat ids are accepted and
        ignored by ``reapply``, which only iterates the managed chats.

        Engagement is stamped on the *transition* into visibility, not for
        everything currently visible: a tab left visible and untouched is not
        continuing engagement, and re-stamping it every heartbeat would make it
        permanently fresh.
        """
        was_visible = self._presence.is_visible(agent_id)
        self._presence.record(agent_id, client_id, state)
        if not was_visible and self._presence.is_visible(agent_id):
            now = self._clock()
            with self._lock:
                self._stamp_engagement_locked(agent_id, now)
        self.reapply()

    def record_message(self, agent_id: str) -> None:
        """Stamp a chat as just-messaged so it ranks newest, then re-tag every chat."""
        now = self._clock()
        with self._lock:
            self._stamp_message_locked(agent_id, now)
        self.reapply()

    def forget_agent(self, agent_id: str) -> None:
        """Drop a destroyed chat's presence so its reports never count again."""
        self._presence.forget_agent(agent_id)

    def record_running_agents(self, running_ids: Iterable[str]) -> None:
        """Record which agents are currently mid-turn, then re-tag if it changed.

        Called from the observe stream on every lifecycle change. Both edges of a
        turn stamp engagement: entering a running state means something addressed
        the chat (for one driven from outside the UI this is the only evidence it
        is still in use), and leaving one means it was active up to that moment --
        without the second stamp a chat that ran for three days would read as
        three days idle the instant its turn ended, and jump straight to the stale
        ceiling. Between the two edges the chat is exempt from the climb entirely.

        The ids may include workers and the primary agent; they are ignored by
        ``reapply``, which only iterates the managed chats.
        """
        now = self._clock()
        with self._lock:
            new_running = set(running_ids)
            if new_running == self._running:
                return
            for agent_id in new_running ^ self._running:
                self._stamp_engagement_locked(agent_id, now)
            self._running = new_running
        self.reapply()

    def reapply(self) -> None:
        """Recompute and write every managed chat's ``oom_score_adj``.

        Snapshots the activity state under the lock, then classifies, ranks by
        recency, resolves each chat's pid, and writes its band -- all outside the
        lock, so a write (or a call into the agent manager / pid registry) never
        blocks a concurrent activity report. Chats with no live process are
        skipped. Idempotent: concurrent reapplies converge on the same result.
        """
        with self._lock:
            running_ids = set(self._running)
            last_message_at = dict(self._last_message_at)
            last_engaged_at = dict(self._last_engaged_at)

        now = self._clock()
        open_ids = self._presence.open_agent_ids()
        visible_ids = self._presence.visible_agent_ids()
        chat_ids = list(self._list_chat_agent_ids())

        # Rank the chats that have been messaged, newest first (rank 0 = most
        # recent). A chat never messaged this session is absent from this map, so
        # ``rank_by_id.get`` returns None and it gets no recency bonus -- it must
        # not be treated as if it were the most recently messaged.
        messaged_newest_first = sorted(
            (cid for cid in chat_ids if cid in last_message_at),
            key=lambda cid: last_message_at[cid],
            reverse=True,
        )
        rank_by_id = {cid: rank for rank, cid in enumerate(messaged_newest_first)}

        for chat_id in chat_ids:
            pid = self._resolve_pid(chat_id)
            if pid is None:
                continue
            is_visible = chat_id in visible_ids
            is_open = chat_id in open_ids
            adj = bands.chat_agent_oom_score_adj(
                is_open=is_open,
                is_visible=is_visible,
                recency_rank=rank_by_id.get(chat_id),
                idle_seconds=self._idle_seconds(chat_id, last_engaged_at, now),
                is_mid_turn=chat_id in running_ids,
            )
            self._set_adj(pid, adj)

    def _idle_seconds(self, chat_id: str, last_engaged_at: dict[str, float], now: float) -> float | None:
        """How long ``chat_id`` has gone without engagement, or None if unknown.

        The chat's own process-start time floors the answer: a chat revived a
        minute ago is fresh whatever its message history says, and for a chat we
        have no recorded engagement for at all it is the only evidence available
        -- an untouched process that started days ago is genuinely abandoned.
        """
        candidates = [
            at for at in (last_engaged_at.get(chat_id), self._resolve_process_started_at(chat_id)) if at is not None
        ]
        if not candidates:
            return None
        return max(0.0, now - max(candidates))

    def _stamp_engagement_locked(self, agent_id: str, at: float) -> None:
        """Record engagement with ``agent_id``, never moving the stamp backwards."""
        previous = self._last_engaged_at.get(agent_id)
        if previous is None or at > previous:
            self._last_engaged_at[agent_id] = at

    def _stamp_message_locked(self, agent_id: str, at: float) -> None:
        """Record a message to ``agent_id`` (which is also engagement with it)."""
        previous = self._last_message_at.get(agent_id)
        if previous is None or at > previous:
            self._last_message_at[agent_id] = at
        self._stamp_engagement_locked(agent_id, at)

    def _run_sweep(self) -> None:
        """Re-tag every managed chat on a slow cadence until stopped.

        The only thing this catches that the event paths do not is the passage of
        time: a chat crosses a staleness threshold without anything happening.
        """
        while not self._sweep_stop.wait(self._sweep_interval_seconds):
            self.reapply()
