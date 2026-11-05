"""Re-tag chat agents' memory-shedding priority from live workspace activity.

The launch wrapper tags every agent's ``oom_score_adj`` once at startup (see
``system/services/oom_priority``). This engine keeps *chat* agents' scores current: a chat
the user is engaged with is protected from an out-of-memory shed, and a chat left
untouched long enough climbs past the worker band so a stale chat is shed before
the worker a live chat just spawned.

Signals, and where each comes from:

- **open** / **visible** -- the chat page's presence in each page instance, reported by
  the page itself through the chat app's presence route and aggregated by the
  ``PresenceTracker`` (open while any instance has an unexpired visible or hidden
  report, visible while any instance's last report says so),
- **messaged** -- a message sent through the chat app's send route; drives a
  recency ranking across all chats, newest-first,
- **running** -- the chat's mngr lifecycle state, pushed in from the observe
  stream via ``record_running_chats``. Entering a running state counts as
  engagement (it is the only evidence of a message sent outside the UI -- by
  ``mngr message`` or by another agent), and staying in one marks the chat
  mid-turn, which suspends its staleness climb until the turn ends. The chat app
  also reports a chat waiting on one of its harness's own background tasks as
  mid-turn, but not one waiting only on ``run_in_background`` commands.

Idle time is measured against the most recent of those events, wall-clock, with
the agent's own process-start time as a floor so a freshly revived chat counts as
fresh. Across a system-interface restart the message stamps are re-seeded from
the durable client-activity log (``seed_last_message_times``), so a restart does
not hand every chat a fresh grace period.

Only chats are managed. Workers and the primary (services) agent are
excluded by the caller's ``list_chat_ids`` (they keep their launch bands --
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

import sys
import threading
import time
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final

from loguru import logger as _loguru_logger
from oom_priority import bands

from imbue.chat.presence import PresenceReport
from imbue.chat.presence import PresenceTracker
from imbue.chat.presence import PresenceTransition
from imbue.chat.primitives import ChatId

# How often the sweep re-evaluates staleness. The ramp is measured in hours, so
# minute-granularity is ample; each pass is a handful of stats and ``/proc``
# writes per chat.
SWEEP_INTERVAL_SECONDS: Final[float] = 60.0

# How long ``stop`` waits for the pass thread to finish its current pass.
_STOP_JOIN_TIMEOUT_SECONDS: Final[float] = 5.0

# Protection is applied by the pass thread after ``record_*`` returns. A pass that starts this
# long after the request that wanted it, or that takes this long, is logged when it starts or
# finishes. This is after-the-fact logging, not a watchdog: a pass that never returns is not
# reported while it is stuck.
_SLOW_PASS_WARN_SECONDS: Final[float] = 5.0


class _PassWorker:
    """One generation of the pass thread, with its own stop and wake events.

    Its own events, not the prioritizer's: a worker that outlives ``stop``'s join finishes its
    current pass and exits, and can neither be revived by a later ``start`` nor take a wake
    meant for the worker that replaced it.
    """

    stop_event: threading.Event
    wake_event: threading.Event
    thread: threading.Thread

    @classmethod
    def build(cls, target: Callable[["_PassWorker"], None]) -> "_PassWorker":
        self = cls.__new__(cls)
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = threading.Thread(target=target, args=(self,), daemon=True, name="oom-chat-sweep")
        return self


class ChatOomPrioritizer:
    """Holds chat activity state and re-tags each chat's ``oom_score_adj``.

    ``list_chat_ids`` returns the ids of the chats to manage (the caller excludes workers and
    the primary agent). ``resolve_pids_by_chat_id`` maps the given chats to their active
    agents' live main-process pids (codex runs two), all in one call, so a pass reads the
    pid registry once rather than once per chat; a chat with no running process may be
    absent or map to an empty list.
    ``set_adj`` writes ``oom_score_adj`` for a pid (best-effort; its return value
    is ignored); None makes an inert prioritizer that still accepts every report but
    computes nothing, writes nothing, and runs no sweep.
    ``resolve_process_started_at`` returns the epoch time at which a
    chat's claude process last started, or None when unknown; it floors the
    engagement clock so a revived chat is never treated as stale. ``clock``
    supplies wall-clock epoch seconds -- absolute, not monotonic, because idle
    time is compared against filesystem mtimes and seeded log timestamps.
    ``presence`` holds the per-instance presence reports the open and visible
    signals are read from; one on the same clock is built when none is given.
    """

    def __init__(
        self,
        *,
        list_chat_ids: Callable[[], Iterable[ChatId]],
        resolve_pids_by_chat_id: Callable[[Sequence[ChatId]], Mapping[ChatId, Sequence[int]]],
        set_adj: Callable[[int, int], bool] | None,
        resolve_process_started_at: Callable[[ChatId], float | None],
        clock: Callable[[], float] = time.time,
        sweep_interval_seconds: float = SWEEP_INTERVAL_SECONDS,
        presence: PresenceTracker | None = None,
        stop_join_timeout_seconds: float = _STOP_JOIN_TIMEOUT_SECONDS,
        start_pass_thread: Callable[[threading.Thread], None] = threading.Thread.start,
    ) -> None:
        self._list_chat_ids = list_chat_ids
        self._resolve_pids_by_chat_id = resolve_pids_by_chat_id
        self._set_adj = set_adj
        self._resolve_process_started_at = resolve_process_started_at
        self._clock = clock
        self._sweep_interval_seconds = sweep_interval_seconds
        # How long ``stop`` waits for the pass thread, and how a pass thread is started:
        # injected so tests can shorten the one and fail or delay the other.
        self._stop_join_timeout_seconds = stop_join_timeout_seconds
        self._start_pass_thread = start_pass_thread
        self._lock = threading.Lock()
        self._presence = presence if presence is not None else PresenceTracker(clock=clock)
        # chat_id -> time of its most recent message, for recency ranking.
        self._last_message_at: dict[ChatId, float] = {}
        # chat_id -> time of its most recent engagement of *any* kind (messaged,
        # switched to, or entered a running state), for the staleness clock.
        self._last_engaged_at: dict[ChatId, float] = {}
        # Chats whose active agent is in a running lifecycle state, i.e. mid-turn.
        self._running: set[ChatId] = set()
        # Guards publishing and withdrawing the pass thread. Never held across a pass or a join,
        # so a slow pass (or a callback that calls ``start``/``stop``) cannot block it.
        self._lifecycle_lock = threading.Lock()
        # Bumped by every ``stop``, so a ``start`` that a ``stop`` overtook does not publish.
        self._lifecycle_generation = 0
        # The running pass thread, published under ``_lock`` only once its thread has started,
        # and withdrawn by ``stop``. While it is None, ``reapply`` runs passes inline.
        self._worker: _PassWorker | None = None
        # Workers that outlived ``stop``'s join; their stop events are set, so they exit after
        # their current pass. The next ``stop`` joins them again.
        self._lingering_workers: list[_PassWorker] = []
        # Held while a pass runs, so at most one runs at a time (see ``reapply``).
        self._pass_lock = threading.Lock()
        # Set under ``_lock`` by every ``reapply``, cleared by the pass that takes it on.
        self._is_pass_requested = False
        # When the oldest still-pending request was made (monotonic), for the slow-pass warning.
        self._pass_requested_at: float | None = None
        # Set under ``_lock`` when a dead pass thread is withdrawn; logged once the lock is released.
        self._is_dead_worker_unreported = False

    def start(self) -> None:
        """Apply the current state once, then start the pass thread, which runs every later pass.

        Separate from construction so tests (and any caller that only wants the
        event-driven behaviour) can drive ``record_*``/``reapply`` directly
        without a background thread. Does nothing without a ``set_adj``, and nothing
        while a pass thread already runs. The first pass runs on the caller's thread and
        outside the lifecycle lock, so a ``stop`` meanwhile neither waits for it nor is
        undone by it. If the thread cannot be started, it raises and nothing is published:
        calls keep running passes inline.
        """
        if self._set_adj is None:
            return
        with self._lifecycle_lock:
            with self._lock:
                if self._live_worker_locked() is not None:
                    return
            generation = self._lifecycle_generation
        self.reapply()
        with self._lifecycle_lock:
            if self._lifecycle_generation != generation:
                return
            with self._lock:
                if self._live_worker_locked() is not None:
                    return
            worker = _PassWorker.build(self._run_passes)
            self._start_pass_thread(worker.thread)
            with self._lock:
                self._worker = worker
        self._report_withdrawn_dead_worker()

    def stop(self) -> None:
        """Stop the pass thread, between passes. Idempotent; safe if ``start`` never ran or failed.

        A pass already running finishes; the join waits up to ``stop_join_timeout_seconds``
        for the thread. Pending work is never dropped: the stopped thread hands it to a
        thread a later ``start`` published, or runs it itself if there is none (see
        ``_run_pending_passes``). Later ``reapply`` calls run inline again.
        """
        with self._lifecycle_lock:
            self._lifecycle_generation += 1
            with self._lock:
                worker = self._worker
                self._worker = None
            if worker is not None:
                worker.stop_event.set()
                worker.wake_event.set()
                self._lingering_workers.append(worker)
            to_join = list(self._lingering_workers)
        for lingering in to_join:
            lingering.thread.join(timeout=self._stop_join_timeout_seconds)
        with self._lifecycle_lock:
            self._lingering_workers = [w for w in self._lingering_workers if w.thread.is_alive()]

    def seed_last_message_times(self, last_message_at_by_chat_id: dict[ChatId, float]) -> None:
        """Seed per-chat last-message times from a durable record, at startup.

        Without this, a system-interface restart would leave every chat with no
        message history and so no recency ranking, and would reset the staleness
        clock to each chat's process-start time -- handing a chat that has been up
        and untouched for days a fresh grace period. Only ever moves a stamp
        forward, so seeding cannot un-engage a chat that was already reported.
        """
        with self._lock:
            for chat_id, messaged_at in last_message_at_by_chat_id.items():
                self._stamp_message_locked(chat_id, messaged_at)

    def record_presence(self, chat_id: ChatId, report: PresenceReport) -> PresenceTransition:
        """Apply one page instance's presence report about one chat, re-tag every chat, and say what it changed.

        The report replaces that instance's standing one unless it is numbered at or
        below it (self-healing: the page's heartbeat corrects any missed one). Non-chat
        ids are accepted and ignored by ``reapply``, which only iterates the managed chats.

        Engagement is stamped on the *transition* into visibility, not for
        everything currently visible: a window left visible and untouched is not
        continuing engagement, and re-stamping it every heartbeat would make it
        permanently fresh.
        """
        transition = self._presence.record(chat_id, report)
        if transition.is_newly_visible:
            now = self._clock()
            with self._lock:
                self._stamp_engagement_locked(chat_id, now)
        self.reapply()
        return transition

    def record_message(self, chat_id: ChatId) -> None:
        """Stamp a chat as just-messaged so it ranks newest, then re-tag every chat."""
        now = self._clock()
        with self._lock:
            self._stamp_message_locked(chat_id, now)
        self.reapply()

    def forget_chat(self, chat_id: ChatId) -> None:
        """Drop a destroyed chat's presence so its reports never count again."""
        self._presence.forget_chat(chat_id)

    def record_running_chats(self, running_ids: Iterable[ChatId]) -> None:
        """Record which chats are currently mid-turn, then re-tag if it changed.

        A chat waiting on one of its harness's own background tasks counts as mid-turn:
        a shed would end the task unreported. One waiting only on ``run_in_background``
        commands does not, since the runner outlives a shed and its report revives the
        chat. Called on every lifecycle change and whenever a chat's pending background
        tasks change. Both edges of a turn stamp engagement: entering a running state
        means something addressed the chat (for one driven from outside the UI this is
        the only evidence it is still in use), and leaving one means it was active up
        to that moment -- without the second stamp a chat that ran for three days would
        read as three days idle the instant its turn ended, and jump straight to the
        stale ceiling. Between the two edges the chat is exempt from the climb entirely.

        The ids may include workers and the primary agent (own chats under the own-chat
        rule); they are ignored by ``reapply``, which only iterates the managed chats.
        """
        now = self._clock()
        with self._lock:
            new_running = set(running_ids)
            if new_running == self._running:
                return
            for chat_id in new_running ^ self._running:
                self._stamp_engagement_locked(chat_id, now)
            self._running = new_running
        self.reapply()

    def reapply(self) -> None:
        """Have every managed chat's ``oom_score_adj`` recomputed and written, by one pass at a time.

        A pass reads ``/proc`` for every registered process, and with dozens of chats that
        is slow enough to matter. When every presence heartbeat ran its own pass on its
        request's thread, with no limit on how many ran at once, the passes slowed each
        other down: in a controlled test (bowei-thoughts-2 session 640) a threaded server
        froze at 0.25 heartbeats/s once one other thread in the process was CPU-busy.

        Once ``start`` has run, this only marks a pass as wanted and wakes the pass thread,
        so it returns at once and no request waits on a pass. So ``record_*`` returning
        means the activity was accepted, not that the band has been written; the pass
        thread writes it moments later. A pass that starts more than ``_SLOW_PASS_WARN_SECONDS``
        after its request, or takes that long, is logged once it starts or finishes (after the
        fact: a pass that never returns is not reported). The pass thread runs one pass
        for each burst of requests, logs a pass that fails reading the registry or ``/proc``, and stops between
        passes. Before ``start`` (and after ``stop``, or if ``start`` failed) the pass runs
        on the caller's thread, still one at a time (see ``_run_pending_passes``): a caller
        that finds another thread running passes returns, and that thread runs its request.

        Does nothing without a ``set_adj``.
        """
        if self._set_adj is None:
            return
        with self._lock:
            if not self._is_pass_requested:
                self._pass_requested_at = time.monotonic()
            self._is_pass_requested = True
            worker = self._live_worker_locked()
        self._report_withdrawn_dead_worker()
        if worker is not None:
            # Enough even if a stop withdraws ``worker`` right now: the flag was set while it was
            # still published, and a withdrawn thread looks at the flag on its way out (and runs
            # the request, or hands it to its successor).
            worker.wake_event.set()
            return
        self._run_pending_passes()

    def _run_pending_passes(self, owner: _PassWorker | None = None) -> None:
        """Run the wanted passes on this thread when it is this thread's turn; never drop one.

        ``owner`` is the pass thread calling it (None for an inline caller). It is this
        thread's turn when it is the published pass thread, or when no pass thread is
        published (an inline caller, or a stopped pass thread finishing up). Otherwise the
        published thread is woken to run the request. That is checked before every pass,
        so a stopped thread hands over to a successor at the next pass boundary.

        With no successor, a stopped thread keeps running passes for as long as requests
        keep arriving while it holds the pass lock, as an inline caller would. It exits
        once none is pending.

        The pass lock is released before the pending flag is read again, so a request that
        lands just as a pass finishes is never left behind: its caller sets the flag before
        it tries the lock, so either it gets the lock itself or the holder sees its flag
        here. A failed pass leaves its request pending and raises.
        """
        while self._pass_lock.acquire(blocking=False):
            try:
                is_my_turn, requested_at = self._take_pending_turn(owner)
                while is_my_turn:
                    started_at = time.monotonic()
                    if requested_at is not None and started_at - requested_at > _SLOW_PASS_WARN_SECONDS:
                        _loguru_logger.warning(
                            "Re-tagging chats' memory priority started {:.1f}s after it was requested",
                            started_at - requested_at,
                        )
                    is_pass_done = False
                    try:
                        self._reapply_once()
                        is_pass_done = True
                    finally:
                        if not is_pass_done:
                            # The pass failed (and its error propagates): keep its request pending.
                            with self._lock:
                                self._is_pass_requested = True
                                if self._pass_requested_at is None:
                                    self._pass_requested_at = requested_at
                    if time.monotonic() - started_at > _SLOW_PASS_WARN_SECONDS:
                        _loguru_logger.warning(
                            "Re-tagging chats' memory priority took {:.1f}s", time.monotonic() - started_at
                        )
                    is_my_turn, requested_at = self._take_pending_turn(owner)
            finally:
                self._pass_lock.release()
                # A dead thread this turn withdrew is logged here, once no lock is held.
                self._report_withdrawn_dead_worker()
            if not self._wake_published_worker_if_pending(owner):
                return

    def _live_worker_locked(self) -> _PassWorker | None:
        """The published pass thread, or None. Called with ``_lock`` held.

        A published thread that is no longer running (it died of an error a pass raised) is
        withdrawn here, so nothing waits on it: passes run inline again, and ``start`` can
        publish a new one. A dying thread also withdraws itself (see ``_run_passes``); this
        covers a thread that ended any other way.
        """
        worker = self._worker
        if worker is not None and not worker.thread.is_alive():
            self._worker = None
            # Logged by ``_report_withdrawn_dead_worker`` once ``_lock`` is released, so a log sink
            # that calls back into this object cannot deadlock on it.
            self._is_dead_worker_unreported = True
            return None
        return worker

    def _report_withdrawn_dead_worker(self) -> None:
        """Log a dead pass thread that ``_live_worker_locked`` withdrew. Called with no lock held."""
        with self._lock:
            is_unreported, self._is_dead_worker_unreported = self._is_dead_worker_unreported, False
        if is_unreported:
            _loguru_logger.warning(
                "The memory-priority pass thread is not running; running passes inline until it is restarted"
            )

    def _take_pending_turn(self, owner: _PassWorker | None) -> tuple[bool, float | None]:
        """Take the pending request if it is ``owner``'s turn to run it; returns (taken, when it was requested).

        It is ``owner``'s turn when it is the published pass thread, or when no pass thread is
        published. Called with the pass lock held.
        """
        with self._lock:
            if not self._is_pass_requested:
                return False, None
            published = self._live_worker_locked()
            if published is not None and published is not owner:
                return False, None
            self._is_pass_requested = False
            requested_at, self._pass_requested_at = self._pass_requested_at, None
            return True, requested_at

    def _wake_published_worker_if_pending(self, owner: _PassWorker | None) -> bool:
        """Hand a pending request to the published pass thread if that is someone other than ``owner``.

        Returns True when the request is still ``owner``'s to run: it is pending, and
        ``owner`` is the published thread or no thread is published.
        """
        with self._lock:
            if not self._is_pass_requested:
                return False
            published = self._live_worker_locked()
        if published is not None and published is not owner:
            published.wake_event.set()
            return False
        return True

    def _reapply_once(self) -> None:
        """One pass: recompute and write every managed chat's ``oom_score_adj``.

        Snapshots the activity state under the lock, then classifies, ranks by
        recency, resolves every chat's pids in one registry read, and writes its band to
        each -- all outside the lock, so a write (or a call into the agent manager / pid
        registry) never blocks a concurrent activity report. Chats with no live process
        are skipped.
        """
        set_adj = self._set_adj
        if set_adj is None:
            return
        with self._lock:
            running_ids = set(self._running)
            last_message_at = dict(self._last_message_at)
            last_engaged_at = dict(self._last_engaged_at)

        now = self._clock()
        open_ids = self._presence.open_chat_ids()
        visible_ids = self._presence.visible_chat_ids()
        chat_ids = list(self._list_chat_ids())

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

        pids_by_chat_id = self._resolve_pids_by_chat_id(chat_ids)
        for chat_id in chat_ids:
            pids = pids_by_chat_id.get(chat_id, ())
            if not pids:
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
            for pid in pids:
                set_adj(pid, adj)

    def _idle_seconds(self, chat_id: ChatId, last_engaged_at: dict[ChatId, float], now: float) -> float | None:
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

    def _stamp_engagement_locked(self, chat_id: ChatId, at: float) -> None:
        """Record engagement with ``chat_id``, never moving the stamp backwards."""
        previous = self._last_engaged_at.get(chat_id)
        if previous is None or at > previous:
            self._last_engaged_at[chat_id] = at

    def _stamp_message_locked(self, chat_id: ChatId, at: float) -> None:
        """Record a message to ``chat_id`` (which is also engagement with it)."""
        previous = self._last_message_at.get(chat_id)
        if previous is None or at > previous:
            self._last_message_at[chat_id] = at
        self._stamp_engagement_locked(chat_id, at)

    def _run_passes(self, worker: _PassWorker) -> None:
        """Run the requested passes, plus one every sweep interval, until ``worker`` is stopped.

        The sweep exists because staleness is the one signal that changes with no event to
        announce it: a chat crosses a staleness threshold without anything happening. A
        pass that fails reading the registry or ``/proc`` (an ``OSError``) is logged and its
        request kept, so the next wake (or the next sweep) retries it. Any other error ends
        the thread, which then withdraws itself: passes run inline on the requests' threads
        again (as before passes moved here), and a later ``start`` publishes a new thread.
        """
        try:
            next_sweep_at = time.monotonic() + self._sweep_interval_seconds
            while not worker.stop_event.is_set():
                worker.wake_event.wait(timeout=max(0.0, next_sweep_at - time.monotonic()))
                worker.wake_event.clear()
                if worker.stop_event.is_set():
                    break
                if time.monotonic() >= next_sweep_at:
                    next_sweep_at = time.monotonic() + self._sweep_interval_seconds
                    with self._lock:
                        if not self._is_pass_requested:
                            self._pass_requested_at = time.monotonic()
                        self._is_pass_requested = True
                try:
                    self._run_pending_passes(owner=worker)
                except OSError as e:
                    _loguru_logger.opt(exception=e).warning("Re-tagging chats' memory priority failed; will retry")
                    # Pace retries of a pass that keeps failing to the sweep, not to the request rate.
                    worker.stop_event.wait(timeout=min(self._sweep_interval_seconds, 5.0))
            # Stopped: hand on (or run) whatever is still pending, so nothing accepted is dropped.
            try:
                self._run_pending_passes(owner=worker)
            except OSError as e:
                _loguru_logger.opt(exception=e).warning("Re-tagging chats' memory priority failed while stopping")
        finally:
            # The error ending this thread, if any, is logged with its traceback before the exit
            # hand-off runs a pass: an error there would become the one the thread reports.
            ending_error = sys.exc_info()[1]
            if not worker.stop_event.is_set():
                # Ended by an error a pass raised (anything but an OSError, which is retried):
                # withdraw this thread so requests run passes inline instead of waking it.
                with self._lock:
                    if self._worker is worker:
                        self._worker = None
                    is_replaced = self._worker is not None
                _loguru_logger.opt(exception=ending_error).error(
                    "The memory-priority pass thread ended unexpectedly; {}",
                    "a new one is running" if is_replaced else "running passes inline until it is restarted",
                )
            elif ending_error is not None:
                _loguru_logger.opt(exception=ending_error).error(
                    "The memory-priority pass thread failed while stopping"
                )
            else:
                # Stopped cleanly: nothing to report.
                pass
            self._hand_off_on_exit(worker)

    def _hand_off_on_exit(self, worker: _PassWorker) -> None:
        """Give whatever is pending to a successor, or try it once here: a request ``worker`` was woken for must not be stranded.

        Runs on ``worker``'s thread as it exits, whichever way. A request may have woken it after
        its last pass, while it was already unwinding from an error. With a live successor, that
        thread is woken; with none, this thread serves pending passes as an inline caller would,
        handing over to a successor at the next pass boundary if one is published. A failure there
        propagates and leaves the request pending for the next call, so a persistent error costs
        one more attempt, not a retry loop. A recovery that succeeds keeps serving requests for as
        long as they keep arriving (like any inline caller), so it is not limited to one pass.
        """
        with self._lock:
            is_pending = self._is_pass_requested
            successor = self._live_worker_locked()
        self._report_withdrawn_dead_worker()
        if not is_pending:
            return
        if successor is not None and successor is not worker:
            successor.wake_event.set()
            return
        self._run_pending_passes()
