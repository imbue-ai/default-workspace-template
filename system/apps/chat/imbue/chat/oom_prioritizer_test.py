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
        self, chat_ids: list[str], pids: dict[str, int | list[int]], sweep_interval_seconds: float = 3600.0
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
    pass_gate: threading.Event, fail_first_pass: bool
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
            raise RuntimeError("registry unreadable")
        return {cid: [10] for cid in cids}

    prioritizer = ChatOomPrioritizer(
        list_chat_ids=lambda: [ChatId("a")],
        resolve_pids_by_chat_id=resolve_pids_by_chat_id,
        set_adj=lambda pid, adj: True,
        resolve_process_started_at=lambda cid: None,
        sweep_interval_seconds=3600.0,
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
    prioritizer.reapply()  # pending behind the running pass
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


def test_a_failed_start_publishes_nothing_and_passes_keep_running_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    class _UnstartableWorker(oom_prioritizer_module._PassWorker):
        def __init__(self, target: Callable[[oom_prioritizer_module._PassWorker], None]) -> None:
            super().__init__(target)

            def refuse() -> None:
                raise RuntimeError("can't start new thread")

            self.thread.start = refuse  # ty: ignore[invalid-assignment]

    monkeypatch.setattr(oom_prioritizer_module, "_PassWorker", _UnstartableWorker)
    h = _Harness(["a"], {"a": 10})
    with pytest.raises(RuntimeError):
        h.prioritizer.start()
    writes_before = len(h.writes)
    h.prioritizer.reapply()
    assert len(h.writes) == writes_before + 1  # ran inline, on this thread
    h.prioritizer.stop()  # must not try to join a thread that never started


def test_a_stop_that_arrives_while_start_is_starting_the_thread_waits_for_it(monkeypatch: pytest.MonkeyPatch) -> None:
    release_start = threading.Event()
    starting = threading.Event()

    class _SlowStartingWorker(oom_prioritizer_module._PassWorker):
        def __init__(self, target: Callable[[oom_prioritizer_module._PassWorker], None]) -> None:
            super().__init__(target)
            real_start = self.thread.start

            def slow_start() -> None:
                starting.set()
                release_start.wait(timeout=5)
                real_start()

            self.thread.start = slow_start  # ty: ignore[invalid-assignment]

    monkeypatch.setattr(oom_prioritizer_module, "_PassWorker", _SlowStartingWorker)
    h = _Harness(["a"], {"a": 10})
    errors: list[BaseException] = []

    def run(fn: Callable[[], None]) -> None:
        try:
            fn()
        except BaseException as e:
            errors.append(e)

    starter = threading.Thread(target=run, args=(h.prioritizer.start,))
    starter.start()
    assert starting.wait(timeout=5)
    stopper = threading.Thread(target=run, args=(h.prioritizer.stop,))
    stopper.start()
    assert not poll_until(lambda: not stopper.is_alive(), timeout=0.3)  # held until start finishes
    release_start.set()
    starter.join(timeout=5)
    stopper.join(timeout=10)
    assert not starter.is_alive() and not stopper.is_alive()
    assert errors == []
    assert h.prioritizer._worker is None
    assert h.prioritizer._lingering_workers == []


def test_a_restart_after_a_timed_out_stop_leaves_one_pass_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oom_prioritizer_module, "_STOP_JOIN_TIMEOUT_SECONDS", 0.1)
    pass_gate = threading.Event()
    prioritizer, _pass_log, pass_entered = _started_prioritizer_with_a_gate(pass_gate, fail_first_pass=False)
    try:
        old_worker = prioritizer._worker
        assert old_worker is not None
        prioritizer.reapply()
        assert pass_entered.wait(timeout=5)
        prioritizer.stop()  # times out: the old worker is still inside its pass
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


def test_a_request_made_after_a_timed_out_stop_is_run_by_the_stopping_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    # The round-3 review's stranded request: the old thread still held the pass lock, so the
    # post-stop caller left it to that thread, which used to exit without running it.
    monkeypatch.setattr(oom_prioritizer_module, "_STOP_JOIN_TIMEOUT_SECONDS", 0.1)
    pass_gate = threading.Event()
    prioritizer, pass_log, pass_entered = _started_prioritizer_with_a_gate(pass_gate, fail_first_pass=False)
    try:
        prioritizer.reapply()
        assert pass_entered.wait(timeout=5)
        prioritizer.stop()  # times out: the old thread is inside its pass, holding the pass lock
        prioritizer.reapply()  # after stop returned: finds the pass lock held and leaves it to the holder
        pass_gate.set()
        assert poll_until(lambda: len(pass_log) == 3 and not prioritizer._is_pass_requested, timeout=5)
    finally:
        pass_gate.set()
        prioritizer.stop()


def test_a_restarted_pass_thread_runs_work_left_pending_across_the_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oom_prioritizer_module, "_STOP_JOIN_TIMEOUT_SECONDS", 0.1)
    pass_gate = threading.Event()
    prioritizer, pass_log, pass_entered = _started_prioritizer_with_a_gate(pass_gate, fail_first_pass=False)
    try:
        prioritizer.reapply()
        assert pass_entered.wait(timeout=5)
        prioritizer.stop()
        prioritizer.reapply()  # pending; the old thread holds the pass lock
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
        assert not stopper.is_alive()  # did not wait for the first pass
    finally:
        release_first_pass.set()
        starter.join(timeout=5)
    assert prioritizer._worker is None  # the stop overtook the start, which therefore published nothing


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
        h.prioritizer._is_pass_requested = True  # accepted, but the thread was never woken for it
    h.prioritizer.stop()
    assert h.prioritizer._is_pass_requested is False
    assert h.registry_reads == reads_before + 1
