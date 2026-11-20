"""Tests for grok's stop and shoulder tap against mngr's ``active`` marker and a scripted Ctrl+C."""

import threading
from pathlib import Path

import pytest

from imbue.chat.activity_state import ACTIVE_MARKER_FILENAME
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.grok.queue_tracker import MAX_DELIVERY_ATTEMPTS
from imbue.chat.harnesses.grok.queue_tracker import get_tracker
from imbue.chat.harnesses.grok.tap import GrokAtomicShoulderTap
from imbue.chat.harnesses.grok.tap import GrokInterruptToComposer
from imbue.chat.harnesses.grok.testing import make_grok_agent_info
from imbue.chat.harnesses.grok.testing import make_grok_agent_state_dir
from imbue.chat.harnesses.grok.watcher import GrokSessionWatcher
from imbue.chat.harnesses.interrupt import PressChord

_SHORT_DEADLINE_SECONDS = 0.3
# How long after mngr refuses a press into a turn grok has just ended its marker goes, well inside the wait.
_MARKER_LAG_SECONDS = 0.1


class _Grok:
    """A scripted grok behind mngr's Ctrl+C rule: a press cancels the turn in flight, or mngr refuses it."""

    def __init__(self, agent_state_dir: Path) -> None:
        self.marker = agent_state_dir / ACTIVE_MARKER_FILENAME
        self.press_count = 0
        self.settle_count = 0
        self.timers: list[threading.Timer] = []

    def begin_turn(self) -> None:
        self.marker.touch()

    def end_turn(self) -> None:
        self.marker.unlink(missing_ok=True)

    def cancelling_press(self) -> bool:
        self.press_count += 1
        self.end_turn()
        return True

    def ignored_press(self) -> bool:
        self.press_count += 1
        return True

    def refused_press(self) -> bool:
        self.press_count += 1
        return False

    def refused_press_before_the_marker_goes(self) -> bool:
        """grok ended the turn on its own just before the press, and mngr's marker goes a moment later."""
        self.press_count += 1
        timer = threading.Timer(_MARKER_LAG_SECONDS, self.end_turn)
        self.timers.append(timer)
        timer.start()
        return False

    def join_timers(self) -> None:
        for timer in self.timers:
            timer.join()

    def settle(self) -> None:
        self.settle_count += 1


def _must_not_restart() -> tuple[bool, str]:
    raise AssertionError("grok's stop never restarts grok")


def _must_not_send(_text: str) -> bool:
    raise AssertionError("the tap never types: the flush worker is grok's only typist")


def _setup(tmp_path: Path, lifecycle_state: str = "RUNNING") -> tuple[AgentInfo, GrokSessionWatcher, _Grok]:
    agent_state_dir = make_grok_agent_state_dir(tmp_path)
    agent_info = make_grok_agent_info(agent_state_dir, agent_state_dir.name, "grok-tap", state=lifecycle_state)
    watcher = GrokSessionWatcher.build(agent_info, lambda _agent_id, _events: None)
    return agent_info, watcher, _Grok(agent_state_dir)


def _hold(agent_info: AgentInfo, *texts: str) -> None:
    for text in texts:
        get_tracker(agent_info.agent_state_dir).enqueue(text, "t0", is_turn_open=True)


def _stop(
    agent_info: AgentInfo, watcher: GrokSessionWatcher, grok: _Grok, press: PressChord, in_flight: str = ""
) -> str:
    stop = GrokInterruptToComposer.build(agent_info)
    stop.turn_end_deadline_seconds = _SHORT_DEADLINE_SECONDS
    return stop.drain_to_composer(watcher, _must_not_restart, grok.settle, press, lambda: in_flight, lambda: False)


def _tap(agent_info: AgentInfo, watcher: GrokSessionWatcher, press: PressChord) -> tuple[str, str | None]:
    tap = GrokAtomicShoulderTap.build(agent_info)
    tap.turn_end_deadline_seconds = _SHORT_DEADLINE_SECONDS
    outcome = tap.tap(watcher, press, _must_not_send)
    assert outcome.block == ""
    return outcome.status, outcome.error_detail


def test_stop_with_no_turn_in_flight_returns_the_held_messages_and_presses_nothing(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path, lifecycle_state="WAITING")
    _hold(agent_info, "first", "second")

    assert _stop(agent_info, watcher, grok, grok.cancelling_press) == "first\nsecond"
    assert grok.press_count == 0
    assert watcher.get_queued_messages() == []


def test_stop_cancels_the_turn_with_one_press_and_returns_held_then_in_flight_in_send_order(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(agent_info, "held")

    assert _stop(agent_info, watcher, grok, grok.cancelling_press, in_flight="in flight") == "held\nin flight"
    assert (grok.press_count, grok.settle_count) == (1, 1)


def test_stop_presses_when_the_marker_is_ahead_of_a_reported_waiting(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path, lifecycle_state="WAITING")
    grok.begin_turn()

    _stop(agent_info, watcher, grok, grok.cancelling_press)

    assert grok.press_count == 1


def test_stop_spares_the_block_a_flush_is_typing(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(agent_info, "being typed")
    queue = get_tracker(agent_info.agent_state_dir)
    _block, claimed, generation = queue.begin_flush()
    _hold(agent_info, "held")

    assert _stop(agent_info, watcher, grok, grok.cancelling_press) == "held"
    queue.finish_flush(claimed, generation, delivered=claimed)
    assert watcher.get_queued_messages() == []


def test_stop_returns_what_a_tap_claimed(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(agent_info, "tapped")
    watcher.claim_queue_for_tap()

    assert _stop(agent_info, watcher, grok, grok.cancelling_press) == "tapped"


@pytest.mark.parametrize("is_press_refused", [pytest.param(True, id="refused"), pytest.param(False, id="ignored")])
def test_a_refused_or_ignored_press_leaves_the_turn_running_and_still_returns_the_held_messages(
    tmp_path: Path, is_press_refused: bool
) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(agent_info, "held")

    assert _stop(agent_info, watcher, grok, grok.refused_press if is_press_refused else grok.ignored_press) == "held"
    assert (grok.press_count, grok.settle_count) == (1, 0)
    assert grok.marker.exists()


def test_the_flush_worker_claims_nothing_while_a_stop_runs(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(agent_info, "held")
    queue = get_tracker(agent_info.agent_state_dir)
    claims_during_the_press: list[tuple[str, ...]] = []

    def press_then_try_to_claim() -> bool:
        grok.cancelling_press()
        claims_during_the_press.append(queue.begin_flush()[1])
        return True

    assert _stop(agent_info, watcher, grok, press_then_try_to_claim) == "held"
    assert claims_during_the_press == [()]


def test_the_tap_cancels_the_turn_and_leaves_the_held_messages_for_the_worker(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(agent_info, "first", "second")

    assert _tap(agent_info, watcher, grok.cancelling_press) == ("flushed", None)
    assert grok.press_count == 1
    assert [entry["content"] for entry in watcher.get_queued_messages()] == ["first", "second"]
    assert get_tracker(agent_info.agent_state_dir).is_sending() is False


def test_the_tap_with_nothing_held_or_no_turn_in_flight_presses_nothing(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    assert _tap(agent_info, watcher, grok.cancelling_press) == ("nothing_queued", None)

    idle_info, idle_watcher, idle_grok = _setup(tmp_path / "idle", lifecycle_state="WAITING")
    _hold(idle_info, "held")
    assert _tap(idle_info, idle_watcher, idle_grok.cancelling_press) == ("no_open_turn", None)
    assert (grok.press_count, idle_grok.press_count) == (0, 0)


def test_the_tap_with_only_spent_messages_held_presses_nothing(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(agent_info, "spent")
    queue = get_tracker(agent_info.agent_state_dir)
    for _attempt in range(MAX_DELIVERY_ATTEMPTS):
        _block, claimed, generation = queue.begin_flush()
        queue.finish_flush(claimed, generation, delivered=())

    assert _tap(agent_info, watcher, grok.cancelling_press) == ("nothing_queued", None)
    assert grok.press_count == 0


def test_a_tap_whose_press_mngr_refuses_as_the_turn_ends_reports_no_open_turn(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(agent_info, "held")

    assert _tap(agent_info, watcher, grok.refused_press_before_the_marker_goes) == ("no_open_turn", None)
    grok.join_timers()


def test_a_tap_whose_press_is_refused_or_ignored_reports_it_and_releases_its_claim(tmp_path: Path) -> None:
    refused_info, refused_watcher, refused_grok = _setup(tmp_path / "refused")
    refused_grok.begin_turn()
    _hold(refused_info, "held")
    status, detail = _tap(refused_info, refused_watcher, refused_grok.refused_press)
    assert (status, detail is not None) == ("press_refused", True)

    ignored_info, ignored_watcher, ignored_grok = _setup(tmp_path / "ignored")
    ignored_grok.begin_turn()
    _hold(ignored_info, "held")
    status, detail = _tap(ignored_info, ignored_watcher, ignored_grok.ignored_press)
    assert (status, detail is not None) == ("not_flushed", True)

    for agent_info in (refused_info, ignored_info):
        assert get_tracker(agent_info.agent_state_dir).is_sending() is False
