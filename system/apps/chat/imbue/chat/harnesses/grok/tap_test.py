"""Tests for grok's stop and shoulder tap against mngr_grok's root-turn files and a scripted Ctrl+C."""

import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.grok.queue_tracker import drop_tracker
from imbue.chat.harnesses.grok.tap import GrokAtomicShoulderTap
from imbue.chat.harnesses.grok.tap import GrokInterruptToComposer
from imbue.chat.harnesses.grok.testing import make_grok_agent_info
from imbue.chat.harnesses.grok.testing import remove_root_turn
from imbue.chat.harnesses.grok.testing import write_root_turn
from imbue.chat.harnesses.grok.watcher import GrokSessionWatcher
from imbue.mngr.errors import AgentNotFoundError

_SESSION = "01a156d6-f927-7091-91a4-f6692a5800da"
_PLACEHOLDER = "pending-wake:01a156db-10ae-70c2-9a0f-a0dff6d5f376"
_SHORT_DEADLINE_SECONDS = 0.3
# How long after mngr refuses a press into a turn grok has ended its root-turn file goes, well inside the wait.
_TURN_FILE_LAG_SECONDS = 0.1


class _Grok:
    """A scripted grok behind mngr's Ctrl+C rule: a press cancels a turn in flight, or mngr refuses it."""

    def __init__(self, agent_state_dir: Path) -> None:
        self.agent_state_dir = agent_state_dir
        self.press_count = 0
        self.restart_count = 0
        self.settle_count = 0
        self.timers: list[threading.Timer] = []

    def begin_turn(self) -> None:
        write_root_turn(self.agent_state_dir, _SESSION, "prompt-1")

    def cancelling_press(self) -> bool:
        self.press_count += 1
        remove_root_turn(self.agent_state_dir, _SESSION)
        return True

    def press_leaving_a_placeholder(self) -> bool:
        self.press_count += 1
        write_root_turn(self.agent_state_dir, _SESSION, _PLACEHOLDER)
        return True

    def ignored_press(self) -> bool:
        self.press_count += 1
        return True

    def refused_press(self) -> bool:
        self.press_count += 1
        return False

    def refused_press_as_the_turn_ends(self) -> bool:
        self.press_count += 1
        remove_root_turn(self.agent_state_dir, _SESSION)
        return False

    def refused_press_before_the_turn_file_goes(self) -> bool:
        """grok ended the turn on its own just before the press, and mngr's file for it goes a moment later."""
        self.press_count += 1
        timer = threading.Timer(_TURN_FILE_LAG_SECONDS, remove_root_turn, args=(self.agent_state_dir, _SESSION))
        self.timers.append(timer)
        timer.start()
        return False

    def press_followed_by_a_wake_turn(self) -> bool:
        """The press cancels the turn, and grok at once starts the follow-up turn of a background shell that ended."""
        self.press_count += 1
        write_root_turn(self.agent_state_dir, _SESSION, "task-completed-shell-1")
        return True

    def join_timers(self) -> None:
        for timer in self.timers:
            timer.join()

    def restart(self) -> tuple[bool, str]:
        self.restart_count += 1
        remove_root_turn(self.agent_state_dir, _SESSION)
        return True, "restarted"

    def settle(self) -> None:
        self.settle_count += 1


def _setup(tmp_path: Path) -> tuple[AgentInfo, GrokSessionWatcher, _Grok]:
    agent_state_dir = tmp_path / "agent-grok-tap"
    agent_state_dir.mkdir()
    drop_tracker(agent_state_dir.name)
    agent_info = make_grok_agent_info(agent_state_dir, agent_state_dir.name, "grok-tap")
    watcher = GrokSessionWatcher.build(agent_info, lambda _agent_id, _events: None)
    return agent_info, watcher, _Grok(agent_state_dir)


def _hold(watcher: GrokSessionWatcher, *texts: str) -> None:
    for text in texts:
        watcher._queue.enqueue(text, "t0", is_turn_open=True)


def _stop(
    agent_info: AgentInfo,
    watcher: GrokSessionWatcher,
    grok: _Grok,
    press: Callable[[], bool],
    restart: Callable[[], tuple[bool, str]] | None = None,
) -> str:
    stop = GrokInterruptToComposer.build(agent_info)
    stop.turn_end_deadline_seconds = _SHORT_DEADLINE_SECONDS
    return stop.drain_to_composer(watcher, restart or grok.restart, grok.settle, press, lambda: "", lambda: False)


def test_stop_with_no_turn_in_flight_returns_the_held_messages_and_presses_nothing(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    _hold(watcher, "first", "second")

    assert _stop(agent_info, watcher, grok, grok.cancelling_press) == "first\nsecond"
    assert (grok.press_count, grok.restart_count) == (0, 0)
    assert watcher.get_queued_messages() == []


def test_stop_never_presses_into_a_pending_wake_placeholder(tmp_path: Path) -> None:
    """A Ctrl+C with no turn to cancel arms grok's quit, and a second one quits it."""
    agent_info, watcher, grok = _setup(tmp_path)
    write_root_turn(grok.agent_state_dir, _SESSION, _PLACEHOLDER)
    _hold(watcher, "held")

    assert _stop(agent_info, watcher, grok, grok.cancelling_press) == "held"
    assert (grok.press_count, grok.restart_count) == (0, 0)


def test_stop_cancels_the_turn_with_one_press_and_spares_a_flush_in_flight(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(watcher, "in flight")
    _block, claimed, _generation = watcher._queue.begin_flush()
    _hold(watcher, "queued")

    assert _stop(agent_info, watcher, grok, grok.cancelling_press) == "queued"
    assert (grok.press_count, grok.restart_count, grok.settle_count) == (1, 0, 1)
    assert [entry["content"] for entry in watcher.get_queued_messages()] == ["in flight"]
    assert len(claimed) == 1


def test_a_cancel_that_leaves_a_pending_wake_placeholder_has_ended_the_turn(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(watcher, "held")

    assert _stop(agent_info, watcher, grok, grok.press_leaving_a_placeholder) == "held"
    assert (grok.press_count, grok.restart_count) == (1, 0)


def test_a_turn_grok_starts_once_the_cancel_landed_is_left_running_and_restarts_nothing(tmp_path: Path) -> None:
    """A restart with --no-resume would throw away the session's context for a turn the stop did end."""
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(watcher, "held")

    assert _stop(agent_info, watcher, grok, grok.press_followed_by_a_wake_turn) == "held"
    assert (grok.press_count, grok.restart_count, grok.settle_count) == (1, 0, 1)


def test_a_refused_press_into_a_turn_that_ended_meanwhile_restarts_nothing(tmp_path: Path) -> None:
    """mngr refuses a press into a grok whose footer no longer shows a turn; restarting that idle grok is wrong."""
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(watcher, "held")

    assert _stop(agent_info, watcher, grok, grok.refused_press_as_the_turn_ends) == "held"
    assert (grok.press_count, grok.restart_count, grok.settle_count) == (1, 0, 0)


def test_a_turn_that_outlives_the_deadline_gets_the_restart_and_every_message_back(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(watcher, "in flight")
    watcher._queue.begin_flush()
    _hold(watcher, "queued")

    assert _stop(agent_info, watcher, grok, grok.ignored_press) == "in flight\nqueued"
    assert (grok.press_count, grok.restart_count) == (1, 1)
    assert watcher.get_queued_messages() == []


def test_a_refused_press_into_a_turn_that_goes_on_gets_the_restart(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(watcher, "held")

    assert _stop(agent_info, watcher, grok, grok.refused_press) == "held"
    assert grok.restart_count == 1


@pytest.mark.parametrize(
    "restart_result", [(True, "restarted"), (False, "grok shows its login screen")], ids=["restarted", "failed"]
)
def test_a_restart_returns_every_message_it_took_and_one_sent_while_it_ran(
    tmp_path: Path, restart_result: tuple[bool, str]
) -> None:
    """Whether or not grok came back, those messages go back to the composer rather than nowhere."""
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(watcher, "taken")

    def restart() -> tuple[bool, str]:
        _hold(watcher, "sent during the restart")
        return restart_result

    assert _stop(agent_info, watcher, grok, grok.ignored_press, restart) == "taken\nsent during the restart"
    assert watcher.get_queued_messages() == []


def test_a_second_stop_right_after_a_landed_press_presses_again_and_restarts_nothing(tmp_path: Path) -> None:
    """mngr spaces two presses itself, so the chat never holds the second one off and leaves the stop a restart."""
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _stop(agent_info, watcher, grok, grok.cancelling_press)
    grok.begin_turn()

    _stop(agent_info, watcher, grok, grok.cancelling_press)

    assert (grok.press_count, grok.restart_count) == (2, 0)


# The shoulder tap


def _tap(agent_info: AgentInfo, watcher: GrokSessionWatcher, press: Callable[[], bool]) -> tuple[str, str | None]:
    def must_not_send(_block: str) -> bool:
        raise AssertionError("the tap never sends: the flush worker is the only typist")

    tap = GrokAtomicShoulderTap.build(agent_info)
    tap.turn_end_deadline_seconds = _SHORT_DEADLINE_SECONDS
    outcome = tap.tap(watcher, press, must_not_send)
    return outcome.status, outcome.error_detail


def test_a_stop_right_after_a_refused_tap_still_has_mngr_press(tmp_path: Path) -> None:
    """A press mngr refused reached nothing, so the stop's own press still cancels the turn."""
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(watcher, "held")
    _tap(agent_info, watcher, grok.refused_press)

    assert _stop(agent_info, watcher, grok, grok.cancelling_press) == "held"
    assert (grok.press_count, grok.restart_count) == (2, 0)


def test_a_tap_with_nothing_queued_presses_nothing(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()

    assert _tap(agent_info, watcher, grok.cancelling_press) == ("nothing_queued", None)
    assert grok.press_count == 0


def test_a_tap_with_no_turn_to_cancel_wakes_the_worker_instead(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    write_root_turn(grok.agent_state_dir, _SESSION, _PLACEHOLDER)
    _hold(watcher, "held")

    assert _tap(agent_info, watcher, grok.cancelling_press) == ("no_open_turn", None)
    assert grok.press_count == 0
    assert watcher._flush_wake.is_set()


def test_a_tap_while_a_flush_holds_the_queue_presses_nothing(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(watcher, "held")
    watcher._queue.begin_flush()

    assert _tap(agent_info, watcher, grok.cancelling_press) == ("send_in_flight", None)
    assert grok.press_count == 0


def test_a_tap_cancels_the_turn_releases_its_claim_and_leaves_delivery_to_the_worker(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(watcher, "deliver me now")

    assert _tap(agent_info, watcher, grok.cancelling_press) == ("flushed", None)
    assert grok.press_count == 1
    assert [(entry["content"], entry["is_sending"]) for entry in watcher.get_queued_messages()] == [
        ("deliver me now", False)
    ]
    assert watcher._flush_wake.is_set()


def test_a_refused_tap_press_releases_the_claim(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(watcher, "held")

    status, error_detail = _tap(agent_info, watcher, grok.refused_press)

    assert status == "press_refused"
    assert error_detail is not None
    assert watcher._queue.is_sending() is False


def test_a_tap_whose_press_raises_releases_the_claim_and_wakes_the_worker(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(watcher, "held")

    def press_into_a_destroyed_agent() -> bool:
        raise AgentNotFoundError("agent-grok-tap")

    with pytest.raises(AgentNotFoundError):
        _tap(agent_info, watcher, press_into_a_destroyed_agent)

    assert watcher._queue.is_sending() is False
    assert watcher._flush_wake.is_set()


def test_a_tap_mngr_refuses_into_a_turn_grok_just_ended_is_no_error(tmp_path: Path) -> None:
    """Measured live: a tap 12 ms after the turn's end answered 500 although the worker delivered 0.7 s later."""
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(watcher, "held")

    try:
        assert _tap(agent_info, watcher, grok.refused_press_before_the_turn_file_goes) == ("no_open_turn", None)
    finally:
        grok.join_timers()
    assert watcher._queue.is_sending() is False
    assert watcher._flush_wake.is_set()


def test_a_tap_whose_cancel_lands_is_flushed_although_grok_starts_another_turn(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(watcher, "held")

    assert _tap(agent_info, watcher, grok.press_followed_by_a_wake_turn) == ("flushed", None)
    assert grok.press_count == 1


def test_a_tap_whose_turn_goes_on_releases_the_claim(tmp_path: Path) -> None:
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(watcher, "held")

    status, _error_detail = _tap(agent_info, watcher, grok.ignored_press)

    assert status == "not_flushed"
    assert watcher._queue.is_sending() is False


def test_the_flush_worker_cannot_send_what_the_stop_returns_into_the_grok_its_cancel_leaves_idle(
    tmp_path: Path,
) -> None:
    """Measured live: the root-turn file goes within 0.1s of the press, and woke the worker before stop took the queue."""
    agent_info, watcher, grok = _setup(tmp_path)
    grok.begin_turn()
    _hold(watcher, "held for the stop")
    sent: list[str] = []
    watcher.set_flush_hooks(lambda _text: True, lambda: True)
    watcher._queue.bind_session_send(lambda text: (sent.append(text), True)[1])

    def press_waking_the_worker() -> bool:
        is_pressed = grok.cancelling_press()
        watcher._attempt_flush()
        return is_pressed

    assert _stop(agent_info, watcher, grok, press_waking_the_worker) == "held for the stop"
    assert sent == []
    assert watcher.get_queued_messages() == []
