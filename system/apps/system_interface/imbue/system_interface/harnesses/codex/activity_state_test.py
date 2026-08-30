"""Unit tests for codex's activity-state derivation (a transcript turn latch) + its tracker."""

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.harnesses.codex.activity import CodexActivityTracker
from imbue.system_interface.harnesses.codex.activity_state import derive
from imbue.system_interface.harnesses.codex.activity_state import turn_open
from imbue.system_interface.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.system_interface.harnesses.events import SpecialEventKind


def _turn_marker(kind: SpecialEventKind) -> dict[str, object]:
    return {"type": SPECIAL_EVENT_TYPE, "kind": kind.value}


def test_turn_open_latches_on_the_latest_marker() -> None:
    assert turn_open([]) is False
    assert turn_open([_turn_marker(SpecialEventKind.TURN_STARTED)]) is True
    # A completed/aborted marker after the start closes the turn.
    assert (
        turn_open([_turn_marker(SpecialEventKind.TURN_STARTED), _turn_marker(SpecialEventKind.TURN_COMPLETED)])
        is False
    )
    assert (
        turn_open([_turn_marker(SpecialEventKind.TURN_STARTED), _turn_marker(SpecialEventKind.TURN_ABORTED)]) is False
    )
    # Non-marker events between start and now don't close the turn (still mid-tool -> open).
    assert turn_open([_turn_marker(SpecialEventKind.TURN_STARTED), {"type": "assistant_message"}]) is True


def test_derive_is_a_turn_latch_not_a_lifecycle() -> None:
    # No open turn -> IDLE, regardless of anything else.
    assert derive(turn_open=False, has_pending_tool_use=False) == ActivityState.IDLE
    # Open turn, no tool -> THINKING (the default while working).
    assert derive(turn_open=True, has_pending_tool_use=False) == ActivityState.THINKING
    # Open turn, tool in flight -> TOOL_RUNNING.
    assert derive(turn_open=True, has_pending_tool_use=True) == ActivityState.TOOL_RUNNING


def test_derive_restart_guard_drops_a_stale_open_turn() -> None:
    # An open turn whose tail predates the current process (a mid-turn kill/restart) reads IDLE.
    assert (
        derive(turn_open=True, has_pending_tool_use=False, tail_event_at=10.0, process_started_at=100.0)
        == ActivityState.IDLE
    )


def test_tracker_observe_and_derive_across_a_turn() -> None:
    tracker = CodexActivityTracker.build()
    # No turn marker yet -> IDLE (NOT driven by any lifecycle signal we pass).
    assert (
        tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None)
        == ActivityState.IDLE
    )

    tracker.observe([_turn_marker(SpecialEventKind.TURN_STARTED)])
    assert (
        tracker.derive(lifecycle_state="WAITING", is_active_marker_present=False, process_started_at=None)
        == ActivityState.THINKING
    )

    # A tool call opens -> TOOL_RUNNING; its result closes -> back to THINKING.
    tracker.observe(
        [
            _turn_marker(SpecialEventKind.TURN_STARTED),
            {"type": "assistant_message", "tool_calls": [{"tool_call_id": "c1"}]},
        ]
    )
    assert (
        tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None)
        == ActivityState.TOOL_RUNNING
    )
    tracker.observe(
        [
            _turn_marker(SpecialEventKind.TURN_STARTED),
            {"type": "assistant_message", "tool_calls": [{"tool_call_id": "c1"}]},
            {"type": "tool_result", "tool_call_id": "c1"},
        ]
    )
    assert (
        tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None)
        == ActivityState.THINKING
    )

    # The turn completes -> IDLE.
    tracker.observe([_turn_marker(SpecialEventKind.TURN_STARTED), _turn_marker(SpecialEventKind.TURN_COMPLETED)])
    assert (
        tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None)
        == ActivityState.IDLE
    )


def test_tracker_reset_closes_the_turn() -> None:
    tracker = CodexActivityTracker.build()
    tracker.observe([_turn_marker(SpecialEventKind.TURN_STARTED)])
    assert (
        tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None)
        == ActivityState.THINKING
    )
    tracker.reset()
    assert (
        tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None)
        == ActivityState.IDLE
    )
