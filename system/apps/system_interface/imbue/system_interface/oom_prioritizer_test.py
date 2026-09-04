"""Unit tests for the chat OOM prioritizer.

The engine is exercised with injected collaborators: a fixed chat-id list, a pid
resolver backed by a dict, a manually-advanced clock, a presence tracker on that
clock, a dict of process-start times, and a capturing ``set_adj`` so the exact
``oom_score_adj`` written per pid is asserted against the band policy.
"""

from oom_priority import bands

from imbue.mngr.utils.polling import poll_until
from imbue.system_interface.oom_prioritizer import ChatOomPrioritizer
from imbue.system_interface.presence import PresenceState
from imbue.system_interface.presence import PresenceTracker

_HOUR = 3600.0


class _Harness:
    """Wires a prioritizer to in-memory fakes and records every band write."""

    def __init__(self, chat_ids: list[str], pids: dict[str, int], sweep_interval_seconds: float = 3600.0) -> None:
        self.chat_ids = chat_ids
        self.pids = pids
        self.writes: list[tuple[int, int]] = []
        # Wall-clock epoch seconds, advanced explicitly by tests. Starts at a
        # plausible epoch rather than 0 so idle arithmetic never goes negative.
        self.now = 1_700_000_000.0
        self.process_started_at: dict[str, float] = {}
        self.prioritizer = ChatOomPrioritizer(
            list_chat_agent_ids=lambda: list(self.chat_ids),
            resolve_pid=lambda cid: self.pids.get(cid),
            set_adj=self._set_adj,
            resolve_process_started_at=self.process_started_at.get,
            clock=lambda: self.now,
            sweep_interval_seconds=sweep_interval_seconds,
            presence=PresenceTracker(clock=lambda: self.now),
        )

    def report(self, agent_id: str, state: PresenceState, client_id: str = "client-1") -> None:
        self.prioritizer.record_presence(agent_id, client_id, state)

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
    h.prioritizer.record_message("a")
    h.advance(60.0)
    h.prioritizer.record_message("b")
    latest = h.latest_adj_by_pid()
    assert latest[20] < latest[10]
    assert latest[20] == _fresh(is_open=False, is_visible=False, recency_rank=0)


def test_a_hidden_page_counts_as_open_but_not_visible() -> None:
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.report("a", PresenceState.HIDDEN)
    assert h.latest_adj_by_pid()[10] == _fresh(is_open=True, is_visible=False, recency_rank=None)


def test_visible_in_any_client_makes_the_chat_visible() -> None:
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.report("a", PresenceState.HIDDEN, client_id="desktop")
    h.report("a", PresenceState.VISIBLE, client_id="phone")
    assert h.latest_adj_by_pid()[10] == _fresh(is_open=True, is_visible=True, recency_rank=None)
    # The phone puts it away; the desktop still has it open.
    h.report("a", PresenceState.CLOSED, client_id="phone")
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
    h.prioritizer.record_message("a")
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
    h.prioritizer.record_message("worker")
    assert set(h.latest_adj_by_pid()) == {10}


def test_a_closed_report_releases_the_chat() -> None:
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.report("a", PresenceState.VISIBLE)
    protected = h.latest_adj_by_pid()[10]
    # The tab is closed; the page's closed report drops the client's presence and ``a``
    # becomes the most-expendable (base) chat again.
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
    h.prioritizer.record_message("stale")
    for _ in range(3 * 24 * 6):
        h.advance(10 * 60.0)
        h.report("stale", PresenceState.HIDDEN)
    h.report("live", PresenceState.VISIBLE)
    h.prioritizer.record_message("live")

    latest = h.latest_adj_by_pid()
    assert latest[10] > bands.WORKER_AGENT
    assert latest[20] < bands.WORKER_AGENT
    # Still never shed before an agent's own subprocesses.
    assert latest[10] < bands.AGENT_SUBPROCESS


def test_idle_time_alone_re_tags_a_chat_with_no_new_reports() -> None:
    # Nothing happens except time passing: the sweep's job. The same chat, same
    # presence, gets progressively more expendable on each reapply.
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.prioritizer.record_message("a")
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
    h.prioritizer.record_message("a")
    h.prioritizer.record_running_agents(["a"])
    h.advance(3 * 24 * _HOUR)
    h.prioritizer.reapply()
    assert h.latest_adj_by_pid()[10] < bands.WORKER_AGENT

    # The turn ends. The chat is no longer exempt, and having been running counts
    # as engagement, so it starts aging from the end of the turn rather than
    # jumping straight to the ceiling.
    h.prioritizer.record_running_agents([])
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

    h.prioritizer.record_running_agents(["a"])
    h.prioritizer.record_running_agents([])
    h.advance(2 * _HOUR)
    h.prioritizer.reapply()
    # Two hours past a turn that ended: aging has started but not gone far.
    assert bands.CHAT_AGENT_BASE < h.latest_adj_by_pid()[10] < bands.WORKER_AGENT


def test_process_start_time_keeps_a_revived_chat_fresh() -> None:
    # ``a``'s last recorded message is ancient, but its process started a minute
    # ago (it was revived), so it must not be treated as abandoned.
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.prioritizer.seed_last_message_times({"a": h.now - 30 * 24 * _HOUR})
    h.process_started_at["a"] = h.now - 60.0
    h.prioritizer.reapply()
    assert h.latest_adj_by_pid()[10] < bands.WORKER_AGENT


def test_an_untouched_long_running_process_ages_out() -> None:
    # The converse: no reported engagement and a process that started days ago is
    # positive evidence of abandonment, not missing evidence.
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.process_started_at["a"] = h.now - 3 * 24 * _HOUR
    h.prioritizer.reapply()
    assert h.latest_adj_by_pid()[10] == bands.CHAT_AGENT_STALE_CEILING


def test_seeded_message_times_restore_recency_across_a_restart() -> None:
    # Rebuilt-from-scratch prioritizer (a system-interface restart): seeding from
    # the durable client-activity log must rank the chats as it did before, rather
    # than treating both as never-messaged.
    h = _Harness(chat_ids=["a", "b"], pids={"a": 10, "b": 20})
    h.prioritizer.seed_last_message_times({"a": h.now - 10 * 60, "b": h.now - 60})
    h.prioritizer.reapply()
    latest = h.latest_adj_by_pid()
    assert latest[20] < latest[10]
    assert latest[20] == _fresh(is_open=False, is_visible=False, recency_rank=0)


def test_seeding_never_moves_an_engagement_stamp_backwards() -> None:
    # A seed carrying an older timestamp than a live report must not un-engage the
    # chat (ordering between startup seeding and the first report is not fixed).
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.prioritizer.record_message("a")
    h.prioritizer.seed_last_message_times({"a": h.now - 30 * 24 * _HOUR})
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
    h.prioritizer.record_running_agents(["a"])
    before = len(h.writes)
    h.prioritizer.record_running_agents(["a"])
    assert len(h.writes) == before


def test_the_sweep_re_tags_as_time_passes_and_stops_cleanly() -> None:
    # Staleness is the one signal with no event to announce it, so the sweep has
    # to be what notices. Nothing is reported here after the initial message; only
    # the clock moves.
    h = _Harness(chat_ids=["a"], pids={"a": 10}, sweep_interval_seconds=0.01)
    h.prioritizer.record_message("a")
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
