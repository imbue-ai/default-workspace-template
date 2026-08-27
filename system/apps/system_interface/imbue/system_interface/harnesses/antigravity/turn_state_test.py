"""Unit tests for agy's turn-open predicate and its cancel-key interlock.

Every rung is bounded on purpose. An unbounded transcript rung is not a smaller bug than the
swallow it replaces -- it is a larger one, because the queue stops making progress at all.
"""

import os
from pathlib import Path

from imbue.system_interface.activity_state import ACTIVE_MARKER_FILENAME
from imbue.system_interface.harnesses.antigravity.turn_state import BUSY_ASSERT_SECONDS
from imbue.system_interface.harnesses.antigravity.turn_state import TAIL_OPEN_SECONDS
from imbue.system_interface.harnesses.antigravity.turn_state import TurnState
from imbue.system_interface.harnesses.antigravity.turn_state import drop_turn_state
from imbue.system_interface.harnesses.antigravity.turn_state import get_turn_state
from imbue.system_interface.harnesses.antigravity.turn_state import is_turn_open_by_tail

# 2026-08-25T00:00:00Z, the timestamp every event below carries.
_TAIL_EPOCH = 1787616000.0
_STAMP = "2026-08-25T00:00:00Z"


def _event(event_type: str, text: str = "") -> dict:
    return {"type": event_type, "text": text, "timestamp": _STAMP}


def _state(events: list[dict], process_started_at: float | None = None) -> TurnState:
    state = TurnState.build()
    state.publish(events, process_started_at)
    return state


# --- the shape half ----------------------------------------------------------------------


def test_a_tool_result_tail_looks_open() -> None:
    assert is_turn_open_by_tail([_event("user_message"), _event("tool_result")]) is True


def test_an_empty_planner_tail_looks_open() -> None:
    """agy's between-tools step. Reading it as a finished answer flickers the dot."""
    assert is_turn_open_by_tail([_event("user_message"), _event("assistant_message", "")]) is True


def test_a_real_answer_tail_looks_closed() -> None:
    assert is_turn_open_by_tail([_event("user_message"), _event("assistant_message", "done")]) is False


def test_an_empty_transcript_looks_closed() -> None:
    assert is_turn_open_by_tail([]) is False


# --- rung 1: a fresh marker asserts busy --------------------------------------------------


def test_a_fresh_marker_means_a_turn_is_open(tmp_path: Path) -> None:
    (tmp_path / ACTIVE_MARKER_FILENAME).write_text("")
    state = _state([_event("assistant_message", "done")])
    assert state.is_hold_required(tmp_path) is True


def test_a_stale_marker_does_not_assert_busy(tmp_path: Path) -> None:
    """A killed process leaves the marker behind. Launch clears it, but never trust its age."""
    marker = tmp_path / ACTIVE_MARKER_FILENAME
    marker.write_text("")
    stale = marker.stat().st_mtime - BUSY_ASSERT_SECONDS - 60
    os.utime(marker, (stale, stale))
    state = _state([_event("assistant_message", "done")])
    assert state.is_hold_required(tmp_path) is False


# --- rungs 2 and 3: the bounded transcript arm --------------------------------------------


def test_a_fresh_open_tail_means_a_turn_is_open(tmp_path: Path) -> None:
    state = _state([_event("tool_result")])
    assert state.is_hold_required(tmp_path, now=_TAIL_EPOCH + 10) is True


def test_an_open_tail_releases_after_our_own_cancel(tmp_path: Path) -> None:
    """THE regression. Measured on agy 1.1.20: a cancelled tool call settles as CANCELED and
    the parser emits a tool_result for it, so the tail reads open forever afterwards. Without
    this rung the first stop an agent receives wedges its queue permanently."""
    state = _state([_event("tool_result")])
    state.note_cancelled(now=_TAIL_EPOCH + 5)
    assert state.is_hold_required(tmp_path, now=_TAIL_EPOCH + 10) is False


def test_an_open_tail_releases_after_a_restart(tmp_path: Path) -> None:
    """agy resumes its own store, so a dead process's tail is still present after relaunch."""
    state = _state([_event("tool_result")], process_started_at=_TAIL_EPOCH + 5)
    assert state.is_hold_required(tmp_path, now=_TAIL_EPOCH + 10) is False


def test_an_open_tail_releases_once_it_is_old_enough(tmp_path: Path) -> None:
    """The backstop for an abandonment we did not cause and cannot see. Progress, not
    conservation: a queue held forever is in exactly one state and still never arrives."""
    state = _state([_event("tool_result")])
    assert state.is_hold_required(tmp_path, now=_TAIL_EPOCH + TAIL_OPEN_SECONDS + 1) is False


def test_a_tail_written_in_the_same_second_as_a_restart_still_counts_as_open(tmp_path: Path) -> None:
    """agy's timestamps have 1s resolution against nanosecond mtimes, so an equal comparison
    reads a genuinely fresh row as abandoned -- and types into a live turn."""
    state = _state([_event("tool_result")], process_started_at=_TAIL_EPOCH + 0.4)
    assert state.is_hold_required(tmp_path, now=_TAIL_EPOCH + 10) is True


def test_a_closed_tail_with_no_marker_is_not_held(tmp_path: Path) -> None:
    state = _state([_event("assistant_message", "done")])
    assert state.is_hold_required(tmp_path, now=_TAIL_EPOCH + 10) is False


def test_an_unwatched_agent_falls_back_to_the_marker(tmp_path: Path) -> None:
    """Treating 'never published' as busy would strand every message sent to an agent whose
    watcher does not exist."""
    state = TurnState.build()
    assert state.is_published() is False
    assert state.is_hold_required(tmp_path, is_watched=False) is False
    (tmp_path / ACTIVE_MARKER_FILENAME).write_text("")
    assert state.is_hold_required(tmp_path, is_watched=False) is True


# --- the cancel-key interlock -------------------------------------------------------------


def test_a_second_press_inside_the_interval_is_refused() -> None:
    """agy treats a double ctrl+c as EXIT, regardless of remapping. This is the only failure
    in the system that destroys the agent process, so it gets a hard interlock."""
    state = TurnState.build()
    assert state.try_claim_press(now=100.0) is True
    assert state.try_claim_press(now=101.0) is False


def test_a_press_is_allowed_once_the_interval_has_passed() -> None:
    state = TurnState.build()
    assert state.try_claim_press(now=100.0) is True
    assert state.try_claim_press(now=120.0) is True


def test_the_interlock_is_shared_between_callers() -> None:
    """Stop and the tap are different objects; the interlock is per AGENT, not per caller."""
    drop_turn_state("agent-shared")
    assert get_turn_state("agent-shared") is get_turn_state("agent-shared")


# --- delivery evidence --------------------------------------------------------------------


def test_user_turn_texts_reports_committed_user_messages_in_order() -> None:
    """A user_message carries its text under ``content``; ``text`` is the assistant's key.
    Reading the wrong one makes every delivery unwitnessable and the block is retyped."""
    state = _state(
        [
            {"type": "user_message", "content": "one", "timestamp": _STAMP},
            _event("assistant_message", "hi"),
            {"type": "user_message", "content": "two", "timestamp": _STAMP},
        ]
    )
    assert state.user_turn_texts() == ("one", "two")
