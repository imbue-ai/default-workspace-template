"""Unit tests for the chat OOM prioritizer.

The engine is exercised with injected collaborators: a fixed chat-id list, a pid
resolver backed by a dict, a manually-advanced clock, a presence tracker on that
clock, a dict of process-start times, and a capturing ``set_adj`` so the exact
``oom_score_adj`` written per pid is asserted against the band policy.
"""

import threading
from collections.abc import Callable
from collections.abc import Sequence

import pytest
from loguru import logger
from oom_priority import bands

import imbue.chat.oom_prioritizer as oom_prioritizer_module
from imbue.chat.oom_prioritizer import ChatOomPrioritizer
from imbue.chat.presence import PresenceReport
from imbue.chat.presence import PresenceState
from imbue.chat.presence import PresenceTracker
from imbue.chat.primitives import ChatId
from imbue.mngr.utils.polling import poll_until

_HOUR = 3600.0


class _Harness:
    """Wires a prioritizer to in-memory fakes and records every band write."""

    def __init__(
        self,
        chat_ids: list[str],
        pids: dict[str, int | list[int]],
        sweep_interval_seconds: float = 3600.0,
        start_pass_thread: Callable[[threading.Thread], None] = threading.Thread.start,
    ) -> None:
        self.chat_ids = [ChatId(chat_id) for chat_id in chat_ids]
        self.pids = pids
        self.writes: list[tuple[int, int]] = []
        # Wall-clock epoch seconds, advanced explicitly by tests. Starts at a
        # plausible epoch rather than 0 so idle arithmetic never goes negative.
        self.now = 1_700_000_000.0
        self.process_started_at: dict[ChatId, float] = {}
        self.reports_sent = 0
        self.registry_reads = 0
        self.prioritizer = ChatOomPrioritizer(
            list_chat_ids=lambda: list(self.chat_ids),
            resolve_pids_by_chat_id=self._resolve_pids_by_chat_id,
            set_adj=self._set_adj,
            resolve_process_started_at=self.process_started_at.get,
            clock=lambda: self.now,
            sweep_interval_seconds=sweep_interval_seconds,
            presence=PresenceTracker(clock=lambda: self.now),
            start_pass_thread=start_pass_thread,
        )

    def report(self, chat_id: str, state: PresenceState, instance_id: str = "page-1") -> None:
        self.reports_sent += 1
        self.prioritizer.record_presence(
            ChatId(chat_id),
            PresenceReport(
                instance_id=instance_id,
                client_id="client-1",
                state=state,
                is_focused=False,
                sequence=self.reports_sent,
            ),
        )

    def _resolve_pids_by_chat_id(self, chat_ids: Sequence[ChatId]) -> dict[ChatId, list[int]]:
        self.registry_reads += 1
        return {chat_id: self._pids_of(chat_id) for chat_id in chat_ids}

    def _pids_of(self, chat_id: ChatId) -> list[int]:
        pids = self.pids.get(chat_id, [])
        return [pids] if isinstance(pids, int) else pids

    def _set_adj(self, pid: int, adj: int) -> bool:
        self.writes.append((pid, adj))
        return True

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def latest_adj_by_pid(self) -> dict[int, int]:
        """The last band written per pid (a reapply rewrites all managed pids)."""
        result: dict[int, int] = {}
        for pid, adj in self.writes:
            result[pid] = adj
        return result


def _fresh(*, is_open: bool, is_visible: bool, recency_rank: int | None) -> int:
    return bands.chat_agent_oom_score_adj(
        is_open=is_open,
        is_visible=is_visible,
        recency_rank=recency_rank,
        idle_seconds=0.0,
        is_mid_turn=False,
    )


def test_open_and_visible_chat_is_more_protected_than_a_closed_one() -> None:
    h = _Harness(chat_ids=["a", "b"], pids={"a": 10, "b": 20})
    h.report("a", PresenceState.VISIBLE)
    latest = h.latest_adj_by_pid()
    # ``a`` is open+visible; ``b`` is closed. Neither messaged, so no recency bonus.
    assert latest[10] == _fresh(is_open=True, is_visible=True, recency_rank=None)
    assert latest[20] == _fresh(is_open=False, is_visible=False, recency_rank=None)
    assert latest[10] < latest[20]


def test_more_recently_messaged_chat_ranks_more_protected() -> None:
    h = _Harness(chat_ids=["a", "b"], pids={"a": 10, "b": 20})
    # Message ``a`` first, then ``b``. Neither has an open tab, so only recency
    # differentiates them and ``b`` (newer) must end up more protected than ``a``.
    h.prioritizer.record_message(ChatId("a"))
    h.advance(60.0)
    h.prioritizer.record_message(ChatId("b"))
    latest = h.latest_adj_by_pid()
    assert latest[20] < latest[10]
    assert latest[20] == _fresh(is_open=False, is_visible=False, recency_rank=0)


def test_a_hidden_page_counts_as_open_but_not_visible() -> None:
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.report("a", PresenceState.HIDDEN)
    assert h.latest_adj_by_pid()[10] == _fresh(is_open=True, is_visible=False, recency_rank=None)


def test_visible_in_any_client_makes_the_chat_visible() -> None:
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.report("a", PresenceState.HIDDEN, instance_id="desktop")
    h.report("a", PresenceState.VISIBLE, instance_id="phone")
    assert h.latest_adj_by_pid()[10] == _fresh(is_open=True, is_visible=True, recency_rank=None)
    # The phone puts it away; the desktop still has it open.
    h.report("a", PresenceState.CLOSED, instance_id="phone")
    assert h.latest_adj_by_pid()[10] == _fresh(is_open=True, is_visible=False, recency_rank=None)


def test_an_unrefreshed_report_expires() -> None:
    # A page that vanished without its pagehide (a crashed tab) stops counting once
    # its heartbeat has been missing for the expiry window.
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.report("a", PresenceState.VISIBLE)
    h.advance(11 * 60.0)
    h.prioritizer.reapply()
    assert h.latest_adj_by_pid()[10] == _fresh(is_open=False, is_visible=False, recency_rank=None)


def test_a_heartbeat_keeps_the_report_alive() -> None:
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.report("a", PresenceState.VISIBLE)
    for _ in range(12):
        h.advance(60.0)
        h.report("a", PresenceState.VISIBLE)
    h.prioritizer.reapply()
    assert h.latest_adj_by_pid()[10] == _fresh(is_open=True, is_visible=True, recency_rank=None)


def test_every_process_of_a_chat_gets_its_band() -> None:
    # A codex chat runs a TUI and an app-server daemon; a shed picks either, so both must
    # carry the chat's current band rather than one keeping its launch band.
    h = _Harness(chat_ids=["a", "b"], pids={"a": [10, 11], "b": 20})
    h.report("a", PresenceState.VISIBLE)
    latest = h.latest_adj_by_pid()
    assert latest[10] == latest[11] == _fresh(is_open=True, is_visible=True, recency_rank=None)
    assert latest[20] == _fresh(is_open=False, is_visible=False, recency_rank=None)


def test_dormant_chat_without_a_live_pid_is_skipped() -> None:
    # ``b`` has no live pid, so it is skipped while ``a`` is tagged.
    h = _Harness(chat_ids=["a", "b"], pids={"a": 10})
    h.report("a", PresenceState.HIDDEN)
    h.report("b", PresenceState.HIDDEN)
    assert set(h.latest_adj_by_pid()) == {10}


def test_revived_chat_is_tagged_on_the_next_reapply() -> None:
    # Dormant: no pid yet, so the first report tags nothing.
    h = _Harness(chat_ids=["a"], pids={})
    h.report("a", PresenceState.VISIBLE)
    h.prioritizer.record_message(ChatId("a"))
    assert h.writes == []
    # A later activity report (e.g. the user messages the now-revived chat) finds
    # its live process and re-tags it -- the re-resolution is idempotent per report.
    h.pids["a"] = 10
    h.prioritizer.reapply()
    # ``a`` was messaged (rank 0), so it earns the recency bonus on top of open+visible.
    assert h.latest_adj_by_pid()[10] == _fresh(is_open=True, is_visible=True, recency_rank=0)


def test_non_chat_ids_in_the_report_are_ignored() -> None:
    # The frontend reports every tab; a worker/primary id that slips into the sets
    # must never be written, because it is not among the managed chat ids.
    h = _Harness(chat_ids=["chat"], pids={"chat": 10, "worker": 99})
    h.report("chat", PresenceState.HIDDEN)
    h.report("worker", PresenceState.VISIBLE)
    h.prioritizer.record_message(ChatId("worker"))
    assert set(h.latest_adj_by_pid()) == {10}


def test_a_closed_report_releases_the_chat() -> None:
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.report("a", PresenceState.VISIBLE)
    protected = h.latest_adj_by_pid()[10]
    # The tab is closed; after the page's closed report ``a`` is neither open nor visible
    # and becomes the most-expendable (base) chat again.
    h.report("a", PresenceState.CLOSED)
    reverted = h.latest_adj_by_pid()[10]
    assert reverted > protected
    assert reverted == _fresh(is_open=False, is_visible=False, recency_rank=None)


def test_a_chat_left_alone_is_shed_before_a_freshly_spawned_worker() -> None:
    """The whole point of the staleness ramp, end to end.

    ``stale`` was last messaged three days ago and its tab is still open; ``live``
    was just messaged and spawned the worker the user actually cares about. The
    stale chat must end up more expendable than the worker band, and the live one
    less.
    """
    h = _Harness(chat_ids=["stale", "live"], pids={"stale": 10, "live": 20})
    h.report("stale", PresenceState.VISIBLE)
    h.prioritizer.record_message(ChatId("stale"))
    for _ in range(3 * 24 * 6):
        h.advance(10 * 60.0)
        h.report("stale", PresenceState.HIDDEN)
    h.report("live", PresenceState.VISIBLE)
    h.prioritizer.record_message(ChatId("live"))

    latest = h.latest_adj_by_pid()
    assert latest[10] > bands.WORKER_AGENT
    assert latest[20] < bands.WORKER_AGENT
    # Still never shed before an agent's own subprocesses.
    assert latest[10] < bands.AGENT_SUBPROCESS


def test_idle_time_alone_re_tags_a_chat_with_no_new_reports() -> None:
    # Nothing happens except time passing: the sweep's job. The same chat, same
    # presence, gets progressively more expendable on each reapply.
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.prioritizer.record_message(ChatId("a"))
    fresh = h.latest_adj_by_pid()[10]
    h.advance(6 * _HOUR)
    h.prioritizer.reapply()
    aged = h.latest_adj_by_pid()[10]
    h.advance(24 * _HOUR)
    h.prioritizer.reapply()
    abandoned = h.latest_adj_by_pid()[10]
    assert fresh < aged < abandoned
    assert abandoned == bands.CHAT_AGENT_STALE_CEILING


def test_a_mid_turn_chat_does_not_age_out() -> None:
    # ``a`` was messaged days ago and has been running ever since (e.g. a long
    # autonomous task another agent kicked off). Shedding it would destroy that
    # work, so it must stay below the worker band until the turn ends.
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.prioritizer.record_message(ChatId("a"))
    h.prioritizer.record_running_chats([ChatId("a")])
    h.advance(3 * 24 * _HOUR)
    h.prioritizer.reapply()
    assert h.latest_adj_by_pid()[10] < bands.WORKER_AGENT

    # The turn ends. The chat is no longer exempt, and having been running counts
    # as engagement, so it starts aging from the end of the turn rather than
    # jumping straight to the ceiling.
    h.prioritizer.record_running_chats([])
    assert h.latest_adj_by_pid()[10] == _fresh(is_open=False, is_visible=False, recency_rank=0)
    h.advance(24 * _HOUR)
    h.prioritizer.reapply()
    assert h.latest_adj_by_pid()[10] == bands.CHAT_AGENT_STALE_CEILING


def test_entering_a_running_state_counts_as_engagement() -> None:
    # A chat messaged outside the UI (by mngr or another agent) never passes through
    # the send route; the lifecycle transition into RUNNING is the only evidence it
    # is still in use, and it must reset the staleness clock.
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.advance(3 * 24 * _HOUR)
    h.prioritizer.reapply()
    # No engagement evidence at all (and no process-start marker) reads as fresh.
    assert h.latest_adj_by_pid()[10] == bands.CHAT_AGENT_BASE

    h.prioritizer.record_running_chats([ChatId("a")])
    h.prioritizer.record_running_chats([])
    h.advance(2 * _HOUR)
    h.prioritizer.reapply()
    # Two hours past a turn that ended: aging has started but not gone far.
    assert bands.CHAT_AGENT_BASE < h.latest_adj_by_pid()[10] < bands.WORKER_AGENT


def test_process_start_time_keeps_a_revived_chat_fresh() -> None:
    # ``a``'s last recorded message is ancient, but its process started a minute
    # ago (it was revived), so it must not be treated as abandoned.
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.prioritizer.seed_last_message_times({ChatId("a"): h.now - 30 * 24 * _HOUR})
    h.process_started_at[ChatId("a")] = h.now - 60.0
    h.prioritizer.reapply()
    assert h.latest_adj_by_pid()[10] < bands.WORKER_AGENT


def test_an_untouched_long_running_process_ages_out() -> None:
    # The converse: no reported engagement and a process that started days ago is
    # positive evidence of abandonment, not missing evidence.
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.process_started_at[ChatId("a")] = h.now - 3 * 24 * _HOUR
    h.prioritizer.reapply()
    assert h.latest_adj_by_pid()[10] == bands.CHAT_AGENT_STALE_CEILING


def test_seeded_message_times_restore_recency_across_a_restart() -> None:
    # Rebuilt-from-scratch prioritizer (a system-interface restart): seeding from
    # the durable client-activity log must rank the chats as it did before, rather
    # than treating both as never-messaged.
    h = _Harness(chat_ids=["a", "b"], pids={"a": 10, "b": 20})
    h.prioritizer.seed_last_message_times({ChatId("a"): h.now - 10 * 60, ChatId("b"): h.now - 60})
    h.prioritizer.reapply()
    latest = h.latest_adj_by_pid()
    assert latest[20] < latest[10]
    assert latest[20] == _fresh(is_open=False, is_visible=False, recency_rank=0)


def test_seeding_never_moves_an_engagement_stamp_backwards() -> None:
    # A seed carrying an older timestamp than a live report must not un-engage the
    # chat (ordering between startup seeding and the first report is not fixed).
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.prioritizer.record_message(ChatId("a"))
    h.prioritizer.seed_last_message_times({ChatId("a"): h.now - 30 * 24 * _HOUR})
    h.prioritizer.reapply()
    assert h.latest_adj_by_pid()[10] == _fresh(is_open=False, is_visible=False, recency_rank=0)


def test_a_visible_tab_is_only_stamped_when_it_becomes_visible() -> None:
    # A tab left visible is not continuing engagement -- re-stamping it on every
    # heartbeat would make it permanently fresh and defeat the ramp entirely.
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.report("a", PresenceState.VISIBLE)
    for _ in range(24 * 60):
        h.advance(60.0)
        h.report("a", PresenceState.VISIBLE)
    assert h.latest_adj_by_pid()[10] == bands.CHAT_AGENT_STALE_CEILING

    # Switching back to it does re-engage.
    h.report("a", PresenceState.HIDDEN)
    h.report("a", PresenceState.VISIBLE)
    assert h.latest_adj_by_pid()[10] == _fresh(is_open=True, is_visible=True, recency_rank=None)


def test_unchanged_running_set_does_not_re_tag() -> None:
    # Lifecycle events fire for reasons unrelated to the running set; a repeat of
    # the same set must not cost a round of /proc writes.
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.prioritizer.record_running_chats([ChatId("a")])
    before = len(h.writes)
    h.prioritizer.record_running_chats([ChatId("a")])
    assert len(h.writes) == before


def test_the_sweep_re_tags_as_time_passes_and_stops_cleanly() -> None:
    # Staleness is the one signal with no event to announce it, so the sweep has
    # to be what notices. Nothing is reported here after the initial message; only
    # the clock moves.
    h = _Harness(chat_ids=["a"], pids={"a": 10}, sweep_interval_seconds=0.01)
    h.prioritizer.record_message(ChatId("a"))
    h.advance(24 * _HOUR)
    try:
        h.prioritizer.start()
        swept = poll_until(lambda: h.latest_adj_by_pid()[10] == bands.CHAT_AGENT_STALE_CEILING)
    finally:
        h.prioritizer.stop()
    assert swept

    # Stopped means stopped: no further writes land after ``stop`` returns.
    settled = len(h.writes)
    assert not poll_until(lambda: len(h.writes) != settled, timeout=0.2)


def test_a_prioritizer_with_no_writer_accepts_every_report_and_touches_no_process() -> None:
    # A secondary chat keeps one of these: its reports arrive through the same routes
    # as the live chat's, but they describe its own windows, so none may reach a process.
    consulted: list[ChatId] = []
    chat_id = ChatId("a")

    def resolve_pids_by_chat_id(cids: Sequence[ChatId]) -> dict[ChatId, list[int]]:
        consulted.extend(cids)
        return {cid: [10] for cid in cids}

    def resolve_process_started_at(cid: ChatId) -> float:
        consulted.append(cid)
        return 1_700_000_000.0

    prioritizer = ChatOomPrioritizer(
        list_chat_ids=lambda: [chat_id],
        resolve_pids_by_chat_id=resolve_pids_by_chat_id,
        set_adj=None,
        resolve_process_started_at=resolve_process_started_at,
        sweep_interval_seconds=0.01,
    )
    prioritizer.seed_last_message_times({chat_id: 1_700_000_000.0})
    prioritizer.record_presence(
        chat_id,
        PresenceReport(
            instance_id="page-1", client_id="client-1", state=PresenceState.VISIBLE, is_focused=False, sequence=1
        ),
    )
    prioritizer.record_message(chat_id)
    prioritizer.record_running_chats([chat_id])
    prioritizer.reapply()
    try:
        prioritizer.start()
        assert not poll_until(lambda: bool(consulted), timeout=0.2)
    finally:
        prioritizer.stop()


def test_a_pass_reads_the_pid_registry_once_however_many_chats_it_manages() -> None:
    # Reading the registry means a read of /proc per registered process; once per chat
    # made a pass cost chats x processes reads, a second or more on a busy workspace.
    h = _Harness(["a", "b", "c", "d", "e"], {"a": 10, "b": 20, "c": 30, "d": 40, "e": 50})
    h.prioritizer.reapply()
    assert h.registry_reads == 1
    assert {pid for pid, _adj in h.writes} == {10, 20, 30, 40, 50}


def _prioritizer_with_a_pass_held_open(
    chat_ids: list[ChatId], last_message_at: dict[ChatId, float]
) -> tuple[ChatOomPrioritizer, threading.Event, threading.Event, list[int], dict[int, int]]:
    """A prioritizer whose first band write blocks until released, so a test can act mid-pass."""
    pass_entered = threading.Event()
    release_pass = threading.Event()
    passes: list[int] = []
    adj_by_pid: dict[int, int] = {}
    pid_by_chat_id = {chat_id: 100 + index for index, chat_id in enumerate(chat_ids)}

    def resolve_pids_by_chat_id(cids: Sequence[ChatId]) -> dict[ChatId, list[int]]:
        passes.append(len(passes) + 1)
        return {cid: [pid_by_chat_id[cid]] for cid in cids}

    def set_adj(pid: int, adj: int) -> bool:
        if len(passes) == 1:
            pass_entered.set()
            release_pass.wait(timeout=5)
        adj_by_pid[pid] = adj
        return True

    prioritizer = ChatOomPrioritizer(
        list_chat_ids=lambda: list(chat_ids),
        resolve_pids_by_chat_id=resolve_pids_by_chat_id,
        set_adj=set_adj,
        resolve_process_started_at=lambda cid: None,
        clock=lambda: 1_700_000_000.0,
        presence=PresenceTracker(clock=lambda: 1_700_000_000.0),
    )
    prioritizer.seed_last_message_times(last_message_at)
    return prioritizer, pass_entered, release_pass, passes, adj_by_pid


def test_reports_made_while_a_pass_runs_return_at_once_and_cost_one_more_pass_between_them() -> None:
    # Every presence heartbeat used to run its own pass, side by side with the rest; they
    # slowed each other down until request threads piled up and the chat app stopped answering.
    chat_ids = [ChatId("a"), ChatId("b")]
    prioritizer, pass_entered, release_pass, passes, _adj_by_pid = _prioritizer_with_a_pass_held_open(chat_ids, {})
    first = threading.Thread(target=prioritizer.reapply)
    first.start()
    try:
        assert pass_entered.wait(timeout=5)
        callers = [threading.Thread(target=prioritizer.reapply) for _ in range(10)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=5)
        assert not any(caller.is_alive() for caller in callers)
        assert passes == [1]
    finally:
        release_pass.set()
        first.join(timeout=5)
    assert passes == [1, 2]


def test_a_message_recorded_while_a_pass_runs_is_written_by_the_pass_that_follows() -> None:
    chat_a = ChatId("a")
    chat_b = ChatId("b")
    prioritizer, pass_entered, release_pass, passes, adj_by_pid = _prioritizer_with_a_pass_held_open(
        [chat_a, chat_b], {chat_a: 1_699_999_000.0, chat_b: 1_699_998_000.0}
    )
    first = threading.Thread(target=prioritizer.reapply)
    first.start()
    try:
        assert pass_entered.wait(timeout=5)
        # b becomes the most recently messaged chat after the running pass took its snapshot.
        prioritizer.record_message(chat_b)
    finally:
        release_pass.set()
        first.join(timeout=5)
    assert passes == [1, 2]
    stale_b = bands.chat_agent_oom_score_adj(
        is_open=False, is_visible=False, recency_rank=1, idle_seconds=2000.0, is_mid_turn=False
    )
    fresh_b = bands.chat_agent_oom_score_adj(
        is_open=False, is_visible=False, recency_rank=0, idle_seconds=0.0, is_mid_turn=False
    )
    assert stale_b != fresh_b
    assert adj_by_pid[101] == fresh_b


class _LockThatRunsBeforeRelease:
    """A lock that runs ``before_release`` once, just before its first release: a request landing as a pass ends."""

    def __init__(self, before_release: Callable[[], None]) -> None:
        self._lock = threading.Lock()
        self._before_release: Callable[[], None] | None = before_release

    def acquire(self, blocking: bool = True) -> bool:
        return self._lock.acquire(blocking)

    def release(self) -> None:
        hook, self._before_release = self._before_release, None
        if hook is not None:
            hook()
        self._lock.release()


def test_a_request_landing_as_a_pass_finishes_is_run_not_dropped() -> None:
    # The review's lost wakeup: the running pass had decided to stop, a new request failed to
    # get the pass lock and left, and nobody ran it.
    h = _Harness(["a"], {"a": 10})
    late_request_results: list[None] = []
    h.prioritizer._pass_lock = _LockThatRunsBeforeRelease(  # ty: ignore[invalid-assignment]
        lambda: late_request_results.append(h.prioritizer.reapply())
    )
    h.prioritizer.reapply()
    assert late_request_results == [None]
    assert h.registry_reads == 2
    assert h.prioritizer._is_pass_requested is False


def _started_prioritizer_with_a_gate(
    pass_gate: threading.Event, fail_first_pass: bool, stop_join_timeout_seconds: float = 5.0
) -> tuple[ChatOomPrioritizer, list[str], threading.Event]:
    """A started prioritizer whose passes block on ``pass_gate``; returns it, its pass log and a 'pass entered' event."""
    pass_log: list[str] = []
    pass_entered = threading.Event()

    def resolve_pids_by_chat_id(cids: Sequence[ChatId]) -> dict[ChatId, list[int]]:
        pass_log.append("pass")
        if len(pass_log) == 1:
            # The pass ``start`` runs itself, on the caller's thread: let it through.
            return {cid: [10] for cid in cids}
        pass_entered.set()
        pass_gate.wait(timeout=5)
        if fail_first_pass and len(pass_log) == 2:
            raise OSError("registry unreadable")
        return {cid: [10] for cid in cids}

    prioritizer = ChatOomPrioritizer(
        list_chat_ids=lambda: [ChatId("a")],
        resolve_pids_by_chat_id=resolve_pids_by_chat_id,
        set_adj=lambda pid, adj: True,
        resolve_process_started_at=lambda cid: None,
        sweep_interval_seconds=3600.0,
        stop_join_timeout_seconds=stop_join_timeout_seconds,
    )
    prioritizer.start()
    return prioritizer, pass_log, pass_entered


def test_once_started_a_request_never_waits_on_a_pass() -> None:
    pass_gate = threading.Event()
    prioritizer, pass_log, pass_entered = _started_prioritizer_with_a_gate(pass_gate, fail_first_pass=False)
    try:
        prioritizer.reapply()
        assert pass_entered.wait(timeout=5)
        # A pass is now running on the pass thread; every further request returns at once.
        callers = [threading.Thread(target=prioritizer.reapply) for _ in range(20)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=2)
        assert not any(caller.is_alive() for caller in callers)
        pass_gate.set()
        # The twenty requests that landed during the pass cost one more pass, not twenty.
        assert poll_until(lambda: len(pass_log) == 3, timeout=5)
        assert not poll_until(lambda: len(pass_log) > 3, timeout=0.3)
    finally:
        pass_gate.set()
        prioritizer.stop()


def test_a_failed_pass_is_logged_and_the_next_request_is_still_served() -> None:
    pass_gate = threading.Event()
    pass_gate.set()
    prioritizer, pass_log, _pass_entered = _started_prioritizer_with_a_gate(pass_gate, fail_first_pass=True)
    try:
        warnings: list[str] = []
        handler_id = logger.add(lambda message: warnings.append(str(message)), level="WARNING")
        try:
            prioritizer.reapply()
            assert poll_until(lambda: any("memory priority failed" in w for w in warnings), timeout=5)
        finally:
            logger.remove(handler_id)
        prioritizer.reapply()
        assert poll_until(lambda: len(pass_log) >= 3, timeout=10)
    finally:
        prioritizer.stop()


def test_stop_ends_the_pass_thread_and_hands_its_pending_pass_on() -> None:
    # A stop never drops accepted work: with no successor thread, the stopping thread runs the
    # pending pass itself before it exits.
    pass_gate = threading.Event()
    prioritizer, pass_log, pass_entered = _started_prioritizer_with_a_gate(pass_gate, fail_first_pass=False)
    worker = prioritizer._worker
    assert worker is not None
    prioritizer.reapply()
    assert pass_entered.wait(timeout=5)
    # pending behind the running pass
    prioritizer.reapply()
    stopper = threading.Thread(target=prioritizer.stop)
    stopper.start()
    pass_gate.set()
    stopper.join(timeout=10)
    assert not stopper.is_alive()
    assert poll_until(lambda: not worker.thread.is_alive(), timeout=5)
    assert len(pass_log) == 3
    assert prioritizer._is_pass_requested is False


def test_a_started_prioritizer_applies_a_messages_band_promptly() -> None:
    # Once started, ``record_*`` returns before the band is written (the pass thread writes it);
    # this pins how soon "moments later" is.
    h = _Harness(["a", "b"], {"a": 10, "b": 20})
    h.prioritizer.seed_last_message_times({ChatId("a"): h.now - 2 * _HOUR, ChatId("b"): h.now - 3 * _HOUR})
    h.prioritizer.start()
    try:
        before = h.latest_adj_by_pid()[20]
        h.prioritizer.record_message(ChatId("b"))
        expected = bands.chat_agent_oom_score_adj(
            is_open=False, is_visible=False, recency_rank=0, idle_seconds=0.0, is_mid_turn=False
        )
        assert expected != before
        assert poll_until(lambda: h.latest_adj_by_pid().get(20) == expected, timeout=2)
    finally:
        h.prioritizer.stop()


def test_a_failed_start_publishes_nothing_and_passes_keep_running_inline() -> None:
    def refuse(thread: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    h = _Harness(["a"], {"a": 10}, start_pass_thread=refuse)
    with pytest.raises(RuntimeError):
        h.prioritizer.start()
    writes_before = len(h.writes)
    h.prioritizer.reapply()
    # ran inline, on this thread
    assert len(h.writes) == writes_before + 1
    # must not try to join a thread that never started
    h.prioritizer.stop()


def test_a_stop_that_arrives_while_start_is_starting_the_thread_waits_for_it() -> None:
    release_start = threading.Event()
    starting = threading.Event()

    def slow_start(thread: threading.Thread) -> None:
        starting.set()
        release_start.wait(timeout=5)
        thread.start()

    h = _Harness(["a"], {"a": 10}, start_pass_thread=slow_start)
    errors: list[RuntimeError] = []

    def run(fn: Callable[[], None]) -> None:
        # The failure this guards against: joining a thread that start has not started yet.
        try:
            fn()
        except RuntimeError as e:
            errors.append(e)

    starter = threading.Thread(target=run, args=(h.prioritizer.start,))
    starter.start()
    assert starting.wait(timeout=5)
    stopper = threading.Thread(target=run, args=(h.prioritizer.stop,))
    stopper.start()
    # held until start finishes
    assert not poll_until(lambda: not stopper.is_alive(), timeout=0.3)
    release_start.set()
    starter.join(timeout=5)
    stopper.join(timeout=10)
    assert not starter.is_alive() and not stopper.is_alive()
    assert errors == []
    assert h.prioritizer._worker is None
    assert h.prioritizer._lingering_workers == []


def test_a_restart_after_a_timed_out_stop_leaves_one_pass_thread() -> None:
    pass_gate = threading.Event()
    prioritizer, _pass_log, pass_entered = _started_prioritizer_with_a_gate(
        pass_gate, fail_first_pass=False, stop_join_timeout_seconds=0.1
    )
    try:
        old_worker = prioritizer._worker
        assert old_worker is not None
        prioritizer.reapply()
        assert pass_entered.wait(timeout=5)
        # times out: the old worker is still inside its pass
        prioritizer.stop()
        assert old_worker.thread.is_alive()
        assert prioritizer._lingering_workers == [old_worker]
        prioritizer.start()
        new_worker = prioritizer._worker
        assert new_worker is not None and new_worker is not old_worker
        pass_gate.set()
        # The old worker finishes its pass and exits; it is not revived by the new start.
        assert poll_until(lambda: not old_worker.thread.is_alive(), timeout=5)
        assert new_worker.thread.is_alive()
    finally:
        pass_gate.set()
        prioritizer.stop()
    assert prioritizer._lingering_workers == []


def test_a_request_made_after_a_timed_out_stop_is_run_by_the_stopping_thread() -> None:
    # The round-3 review's stranded request: the old thread still held the pass lock, so the
    # post-stop caller left it to that thread, which used to exit without running it.
    pass_gate = threading.Event()
    prioritizer, pass_log, pass_entered = _started_prioritizer_with_a_gate(
        pass_gate, fail_first_pass=False, stop_join_timeout_seconds=0.1
    )
    try:
        prioritizer.reapply()
        assert pass_entered.wait(timeout=5)
        # times out: the old thread is inside its pass, holding the pass lock
        prioritizer.stop()
        # after stop returned: finds the pass lock held and leaves it to the holder
        prioritizer.reapply()
        pass_gate.set()
        assert poll_until(lambda: len(pass_log) == 3 and not prioritizer._is_pass_requested, timeout=5)
    finally:
        pass_gate.set()
        prioritizer.stop()


def test_a_restarted_pass_thread_runs_work_left_pending_across_the_restart() -> None:
    pass_gate = threading.Event()
    prioritizer, pass_log, pass_entered = _started_prioritizer_with_a_gate(
        pass_gate, fail_first_pass=False, stop_join_timeout_seconds=0.1
    )
    try:
        prioritizer.reapply()
        assert pass_entered.wait(timeout=5)
        prioritizer.stop()
        # pending; the old thread holds the pass lock
        prioritizer.reapply()
        restarter = threading.Thread(target=prioritizer.start)
        restarter.start()
        restarter.join(timeout=5)
        assert not restarter.is_alive()
        pass_gate.set()
        # No further request: the pending work is run (by the old thread handing it to the new one).
        assert poll_until(lambda: not prioritizer._is_pass_requested and len(pass_log) >= 3, timeout=5)
    finally:
        pass_gate.set()
        prioritizer.stop()


def test_stop_does_not_wait_behind_starts_first_pass_and_start_then_stays_stopped() -> None:
    release_first_pass = threading.Event()
    first_pass_entered = threading.Event()

    def resolve_pids_by_chat_id(cids: Sequence[ChatId]) -> dict[ChatId, list[int]]:
        first_pass_entered.set()
        release_first_pass.wait(timeout=10)
        return {cid: [10] for cid in cids}

    prioritizer = ChatOomPrioritizer(
        list_chat_ids=lambda: [ChatId("a")],
        resolve_pids_by_chat_id=resolve_pids_by_chat_id,
        set_adj=lambda pid, adj: True,
        resolve_process_started_at=lambda cid: None,
        sweep_interval_seconds=3600.0,
    )
    starter = threading.Thread(target=prioritizer.start)
    starter.start()
    try:
        assert first_pass_entered.wait(timeout=5)
        stopper = threading.Thread(target=prioritizer.stop)
        stopper.start()
        stopper.join(timeout=2)
        # did not wait for the first pass
        assert not stopper.is_alive()
    finally:
        release_first_pass.set()
        starter.join(timeout=5)
    # the stop overtook the start, which therefore published nothing
    assert prioritizer._worker is None


def test_a_callback_may_stop_the_prioritizer_during_starts_first_pass() -> None:
    # Lifecycle re-entry from a callback used to self-deadlock on the lifecycle lock.
    holder: list[ChatOomPrioritizer] = []

    def resolve_pids_by_chat_id(cids: Sequence[ChatId]) -> dict[ChatId, list[int]]:
        holder[0].stop()
        return {cid: [10] for cid in cids}

    prioritizer = ChatOomPrioritizer(
        list_chat_ids=lambda: [ChatId("a")],
        resolve_pids_by_chat_id=resolve_pids_by_chat_id,
        set_adj=lambda pid, adj: True,
        resolve_process_started_at=lambda cid: None,
        sweep_interval_seconds=3600.0,
    )
    holder.append(prioritizer)
    starter = threading.Thread(target=prioritizer.start, daemon=True)
    starter.start()
    starter.join(timeout=5)
    assert not starter.is_alive()
    assert prioritizer._worker is None


def test_work_pending_when_the_pass_thread_is_told_to_stop_is_run_before_it_exits() -> None:
    # A request that woke the thread just as stop told it to exit: the thread sees the stop at the
    # top of its loop, and must still run (or hand on) the pending pass rather than drop it.
    h = _Harness(["a"], {"a": 10})
    h.prioritizer.start()
    reads_before = h.registry_reads
    with h.prioritizer._lock:
        # accepted, but the thread was never woken for it
        h.prioritizer._is_pass_requested = True
    h.prioritizer.stop()
    assert h.prioritizer._is_pass_requested is False
    assert h.registry_reads == reads_before + 1


def _gated_prioritizer(
    gate_pass_numbers: set[int], fail_pass_numbers: set[int], stop_join_timeout_seconds: float = 5.0
) -> tuple[ChatOomPrioritizer, list[threading.Thread], threading.Event, threading.Event]:
    """A prioritizer whose numbered passes block on a gate (and optionally fail); records each pass's thread."""
    pass_threads: list[threading.Thread] = []
    gate = threading.Event()
    gated_pass_entered = threading.Event()

    def resolve_pids_by_chat_id(cids: Sequence[ChatId]) -> dict[ChatId, list[int]]:
        pass_threads.append(threading.current_thread())
        number = len(pass_threads)
        if number in gate_pass_numbers:
            gated_pass_entered.set()
            gate.wait(timeout=10)
        if number in fail_pass_numbers:
            raise OSError("registry unreadable")
        return {cid: [10] for cid in cids}

    prioritizer = ChatOomPrioritizer(
        list_chat_ids=lambda: [ChatId("a")],
        resolve_pids_by_chat_id=resolve_pids_by_chat_id,
        set_adj=lambda pid, adj: True,
        resolve_process_started_at=lambda cid: None,
        sweep_interval_seconds=3600.0,
        stop_join_timeout_seconds=stop_join_timeout_seconds,
    )
    return prioritizer, pass_threads, gate, gated_pass_entered


def test_a_stopped_thread_hands_new_work_to_its_replacement_at_the_next_pass_boundary() -> None:
    # Round-4 finding: the stopped thread kept taking passes after a replacement was published.
    prioritizer, pass_threads, gate, gated_pass_entered = _gated_prioritizer({2}, set(), stop_join_timeout_seconds=0.1)
    # pass 1, inline on this thread
    prioritizer.start()
    old_worker = prioritizer._worker
    assert old_worker is not None
    try:
        # pass 2, on the old thread, held
        prioritizer.reapply()
        assert gated_pass_entered.wait(timeout=5)
        # times out; the old thread still holds the pass lock
        prioritizer.stop()
        # pending behind the old thread
        prioritizer.reapply()
        restarter = threading.Thread(target=prioritizer.start, name="restarter")
        restarter.start()
        restarter.join(timeout=5)
        new_worker = prioritizer._worker
        assert new_worker is not None and new_worker is not old_worker
        gate.set()
        assert poll_until(lambda: not prioritizer._is_pass_requested and len(pass_threads) >= 3, timeout=5)
        for _ in range(5):
            prioritizer.reapply()
            poll_until(lambda: not prioritizer._is_pass_requested, timeout=5)
        assert poll_until(lambda: not old_worker.thread.is_alive(), timeout=5)
        # Every pass after the old thread's held one ran on the replacement thread.
        assert pass_threads[2:] and all(thread is new_worker.thread for thread in pass_threads[2:])
    finally:
        gate.set()
        prioritizer.stop()


def test_a_request_that_races_a_restart_never_runs_a_pass_on_the_requesting_thread() -> None:
    # Round-4 finding: a request that woke the old thread just as stop and start ran fell back to
    # running a pass inline although a replacement was published.
    prioritizer, pass_threads, _gate, _entered = _gated_prioritizer(set(), set())
    prioritizer.start()
    old_worker = prioritizer._worker
    assert old_worker is not None
    real_set = old_worker.wake_event.set
    has_restarted: list[bool] = []

    def restart_then_wake() -> None:
        # Only the request's own wake restarts (stop wakes the old thread too, through this same method).
        if not has_restarted:
            has_restarted.append(True)
            # The restart happens elsewhere (start runs its own first pass on its caller's thread).
            restarter = threading.Thread(target=lambda: (prioritizer.stop(), prioritizer.start()), name="restarter")
            restarter.start()
            restarter.join(timeout=10)
        real_set()

    old_worker.wake_event.set = restart_then_wake  # ty: ignore[invalid-assignment]
    try:
        caller = threading.Thread(target=prioritizer.reapply, name="requesting-thread")
        caller.start()
        caller.join(timeout=10)
        assert not caller.is_alive()
        assert poll_until(lambda: not prioritizer._is_pass_requested, timeout=5)
        assert "requesting-thread" not in [thread.name for thread in pass_threads]
    finally:
        prioritizer.stop()


def test_a_pass_that_fails_while_the_thread_stops_is_handed_to_the_replacement() -> None:
    # Round-4 finding: the failure skipped the hand-off and left the replacement asleep with work pending.
    prioritizer, pass_threads, gate, gated_pass_entered = _gated_prioritizer({3}, {3}, stop_join_timeout_seconds=0.1)
    # pass 1, inline
    prioritizer.start()
    old_worker = prioritizer._worker
    assert old_worker is not None
    try:
        # the old thread exits cleanly; nothing pending
        prioritizer.stop()
        assert poll_until(lambda: not old_worker.thread.is_alive(), timeout=5)
        # pass 2, inline on this thread (nothing published)
        prioritizer.reapply()
        # Now a stopped thread that is running a pass (pass 3, held then failing) while a
        # replacement gets published: drive it through the stopping path directly.
        stopping_worker = oom_prioritizer_module._PassWorker.build(prioritizer._run_passes)
        stopping_worker.stop_event.set()
        with prioritizer._lock:
            prioritizer._is_pass_requested = True
        stopping_worker.thread.start()
        assert gated_pass_entered.wait(timeout=5)
        # replacement published while pass 3 runs
        prioritizer.start()
        replacement = prioritizer._worker
        assert replacement is not None
        # pass 3 fails; its request must go to the replacement without another request
        gate.set()
        assert poll_until(lambda: not prioritizer._is_pass_requested, timeout=5)
        assert pass_threads[-1] is replacement.thread
    finally:
        gate.set()
        prioritizer.stop()


def test_a_failed_pass_keeps_its_request_pending_for_the_next_one() -> None:
    prioritizer, pass_threads, _gate, _entered = _gated_prioritizer(set(), {2})
    prioritizer.reapply()
    with pytest.raises(OSError):
        prioritizer.reapply()
    assert prioritizer._is_pass_requested is True
    prioritizer.reapply()
    assert prioritizer._is_pass_requested is False
    assert len(pass_threads) == 3


def test_a_replacement_published_during_a_stopped_threads_extra_pass_takes_over_after_it() -> None:
    # The round-5 review's stronger schedule: the old thread is already running an extra pass
    # after stop (no successor yet) when the replacement is published.
    gates = {2: threading.Event(), 3: threading.Event()}
    entered = {2: threading.Event(), 3: threading.Event()}
    pass_threads: list[threading.Thread] = []

    def resolve_pids_by_chat_id(cids: Sequence[ChatId]) -> dict[ChatId, list[int]]:
        pass_threads.append(threading.current_thread())
        number = len(pass_threads)
        if number in gates:
            entered[number].set()
            gates[number].wait(timeout=10)
        return {cid: [10] for cid in cids}

    prioritizer = ChatOomPrioritizer(
        list_chat_ids=lambda: [ChatId("a")],
        resolve_pids_by_chat_id=resolve_pids_by_chat_id,
        set_adj=lambda pid, adj: True,
        resolve_process_started_at=lambda cid: None,
        sweep_interval_seconds=3600.0,
        stop_join_timeout_seconds=0.1,
    )
    prioritizer.start()
    old_worker = prioritizer._worker
    assert old_worker is not None
    try:
        prioritizer.reapply()
        assert entered[2].wait(timeout=5)
        prioritizer.stop()
        # Pending behind pass 2; with no successor the old thread runs it as pass 3.
        prioritizer.reapply()
        gates[2].set()
        assert entered[3].wait(timeout=5)
        assert pass_threads[2] is old_worker.thread
        restarter = threading.Thread(target=prioritizer.start, name="restarter")
        restarter.start()
        restarter.join(timeout=5)
        new_worker = prioritizer._worker
        assert new_worker is not None and new_worker is not old_worker
        for _ in range(3):
            prioritizer.reapply()
        gates[3].set()
        assert poll_until(lambda: not prioritizer._is_pass_requested and len(pass_threads) >= 4, timeout=5)
        assert poll_until(lambda: not old_worker.thread.is_alive(), timeout=5)
        assert all(thread is new_worker.thread for thread in pass_threads[3:])
    finally:
        for gate in gates.values():
            gate.set()
        prioritizer.stop()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_pass_that_raises_an_unexpected_error_does_not_stop_later_bands_being_written() -> None:
    # Only OSError is retried on the pass thread; any other error ends it. That must not leave
    # every later request waking a dead thread with no band ever written again.
    writes: list[tuple[int, int]] = []
    passes: list[threading.Thread] = []

    def resolve_pids_by_chat_id(cids: Sequence[ChatId]) -> dict[ChatId, list[int]]:
        passes.append(threading.current_thread())
        if len(passes) == 2:
            raise ValueError("a bug in a callback")
        return {cid: [10] for cid in cids}

    def set_adj(pid: int, adj: int) -> bool:
        writes.append((pid, adj))
        return True

    prioritizer = ChatOomPrioritizer(
        list_chat_ids=lambda: [ChatId("a")],
        resolve_pids_by_chat_id=resolve_pids_by_chat_id,
        set_adj=set_adj,
        resolve_process_started_at=lambda cid: None,
        sweep_interval_seconds=3600.0,
    )
    prioritizer.start()
    try:
        first_worker = prioritizer._worker
        assert first_worker is not None
        # Pass 2 raises ValueError on the pass thread, which ends it.
        prioritizer.reapply()
        assert poll_until(lambda: not first_worker.thread.is_alive(), timeout=5)
        assert prioritizer._worker is None
        writes_before = len(writes)
        prioritizer.record_message(ChatId("a"))
        # Ran inline, on this thread: the band is written by the time it returns.
        assert len(writes) == writes_before + 1
        assert prioritizer._is_pass_requested is False
        # And a later start brings a pass thread back.
        prioritizer.start()
        assert prioritizer._worker is not None and prioritizer._worker.thread.is_alive()
    finally:
        prioritizer.stop()


def test_a_published_pass_thread_that_is_no_longer_running_is_withdrawn_and_passes_run_inline() -> None:
    h = _Harness(["a"], {"a": 10})
    dead = oom_prioritizer_module._PassWorker.build(lambda worker: None)
    dead.thread.start()
    dead.thread.join(timeout=5)
    with h.prioritizer._lock:
        h.prioritizer._worker = dead
    writes_before = len(h.writes)
    h.prioritizer.reapply()
    assert len(h.writes) == writes_before + 1
    assert h.prioritizer._worker is None


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_request_accepted_while_a_pass_thread_is_dying_is_still_run() -> None:
    # Round-6 finding: the request saw the thread alive, woke it and returned; the thread then
    # withdrew without running it, and nothing ran it until another request came.
    failing = threading.Event()
    request_done = threading.Event()
    passes: list[threading.Thread] = []

    def resolve_pids_by_chat_id(cids: Sequence[ChatId]) -> dict[ChatId, list[int]]:
        passes.append(threading.current_thread())
        if len(passes) == 2:
            failing.set()
            request_done.wait(timeout=10)
            raise ValueError("a bug in a callback")
        return {cid: [10] for cid in cids}

    prioritizer = ChatOomPrioritizer(
        list_chat_ids=lambda: [ChatId("a")],
        resolve_pids_by_chat_id=resolve_pids_by_chat_id,
        set_adj=lambda pid, adj: True,
        resolve_process_started_at=lambda cid: None,
        sweep_interval_seconds=3600.0,
    )
    prioritizer.start()
    worker = prioritizer._worker
    assert worker is not None
    try:
        prioritizer.reapply()
        assert failing.wait(timeout=5)
        # Accepted while the pass thread is about to die: it wakes that thread and returns.
        prioritizer.record_message(ChatId("a"))
        request_done.set()
        # No further request: the dying thread runs it on its way out.
        assert poll_until(lambda: not worker.thread.is_alive(), timeout=5)
        assert poll_until(lambda: not prioritizer._is_pass_requested, timeout=5)
        assert len(passes) == 3
    finally:
        request_done.set()
        prioritizer.stop()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_an_unexpected_error_in_a_stopping_threads_last_pass_is_handed_to_the_replacement() -> None:
    gate = threading.Event()
    entered = threading.Event()
    passes: list[threading.Thread] = []

    def resolve_pids_by_chat_id(cids: Sequence[ChatId]) -> dict[ChatId, list[int]]:
        passes.append(threading.current_thread())
        if len(passes) == 2:
            entered.set()
            gate.wait(timeout=10)
            raise ValueError("a bug in a callback")
        return {cid: [10] for cid in cids}

    prioritizer = ChatOomPrioritizer(
        list_chat_ids=lambda: [ChatId("a")],
        resolve_pids_by_chat_id=resolve_pids_by_chat_id,
        set_adj=lambda pid, adj: True,
        resolve_process_started_at=lambda cid: None,
        sweep_interval_seconds=3600.0,
        stop_join_timeout_seconds=0.1,
    )
    # Pass 1, inline (nothing published).
    prioritizer.reapply()
    stopping_worker = oom_prioritizer_module._PassWorker.build(prioritizer._run_passes)
    stopping_worker.stop_event.set()
    with prioritizer._lock:
        prioritizer._is_pass_requested = True
    stopping_worker.thread.start()
    try:
        assert entered.wait(timeout=5)
        prioritizer.start()
        replacement = prioritizer._worker
        assert replacement is not None
        gate.set()
        # The stopping thread's pass raises ValueError; the replacement runs the request at once,
        # well inside its one-hour sweep.
        assert poll_until(lambda: not prioritizer._is_pass_requested, timeout=5)
        assert passes[-1] is replacement.thread
    finally:
        gate.set()
        prioritizer.stop()


def test_a_log_sink_that_calls_back_in_does_not_deadlock_on_the_dead_thread_warning() -> None:
    # The warning used to be logged with the state lock held, so a sink calling back into the
    # prioritizer blocked on it. The sink calls back on a side thread and records whether that
    # call got through while the warning was being logged (the test cannot hang either way).
    h = _Harness(["a"], {"a": 10})
    dead = oom_prioritizer_module._PassWorker.build(lambda worker: None)
    dead.thread.start()
    dead.thread.join(timeout=5)
    with h.prioritizer._lock:
        h.prioritizer._worker = dead
    callback_was_blocked: list[bool] = []

    def reentrant_sink(message: object) -> None:
        if "pass thread is not running" in str(message):
            callback = threading.Thread(target=h.prioritizer.reapply, daemon=True)
            callback.start()
            callback.join(timeout=1.0)
            callback_was_blocked.append(callback.is_alive())

    handler_id = logger.add(reentrant_sink, level="WARNING")
    try:
        h.prioritizer.reapply()
    finally:
        logger.remove(handler_id)
    assert callback_was_blocked == [False]
    assert h.prioritizer._worker is None


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_the_error_that_ends_a_pass_thread_is_logged_with_its_traceback() -> None:
    # A failure in the exit hand-off's recovery pass would replace the original error as the one
    # the thread reports, so the original is logged first.
    passes: list[int] = []

    def resolve_pids_by_chat_id(cids: Sequence[ChatId]) -> dict[ChatId, list[int]]:
        passes.append(len(passes) + 1)
        if len(passes) == 2:
            raise ValueError("the original bug")
        if len(passes) == 3:
            raise SystemExit("recovery failed too")
        return {cid: [10] for cid in cids}

    prioritizer = ChatOomPrioritizer(
        list_chat_ids=lambda: [ChatId("a")],
        resolve_pids_by_chat_id=resolve_pids_by_chat_id,
        set_adj=lambda pid, adj: True,
        resolve_process_started_at=lambda cid: None,
        sweep_interval_seconds=3600.0,
    )
    errors: list[str] = []
    handler_id = logger.add(lambda message: errors.append(str(message)), level="ERROR")
    try:
        prioritizer.start()
        worker = prioritizer._worker
        assert worker is not None
        # Pass 2 raises ValueError; the exit recovery (pass 3) raises SystemExit.
        prioritizer.reapply()
        assert poll_until(lambda: not worker.thread.is_alive(), timeout=5)
    finally:
        logger.remove(handler_id)
        prioritizer.stop()
    assert any("ended unexpectedly" in e and "ValueError: the original bug" in e for e in errors)
