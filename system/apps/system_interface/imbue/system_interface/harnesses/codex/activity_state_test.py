from typing import Any

import pytest

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.harnesses.codex.activity_state import current_open_turn_id
from imbue.system_interface.harnesses.codex.activity_state import turn_open
from imbue.system_interface.harnesses.codex.activity_state import derive


def _started(turn_id: str) -> dict[str, Any]:
    return {"type": "special", "kind": "turn_started", "turn_id": turn_id}


def _completed(turn_id: str) -> dict[str, Any]:
    return {"type": "special", "kind": "turn_completed", "turn_id": turn_id}


def _aborted(turn_id: str) -> dict[str, Any]:
    return {"type": "special", "kind": "turn_aborted", "turn_id": turn_id}


@pytest.mark.parametrize(
    "events, expected",
    [
        pytest.param([], None, id="empty_has_no_open_turn"),
        pytest.param([_started("t1")], "t1", id="started_is_open"),
        pytest.param([_started("t1"), _completed("t1")], None, id="completed_clears"),
        pytest.param([_started("t1"), _aborted("t1")], None, id="aborted_clears"),
        # A completed boundary for a DIFFERENT turn does not clear the open one.
        pytest.param([_started("t1"), _completed("t2")], "t1", id="stale_boundary_ignored"),
        # Second turn opens after the first closes -> the newest open id wins.
        pytest.param([_started("t1"), _completed("t1"), _started("t2")], "t2", id="reopen_new_id"),
        # Non-boundary events between the start and end are skipped.
        pytest.param(
            [_started("t1"), {"type": "assistant_message"}, {"type": "tool_result"}],
            "t1",
            id="mid_turn_still_open",
        ),
        pytest.param([{"type": "assistant_message"}], None, id="no_markers_has_no_open_turn"),
        # A turn_started without a turn_id cannot be gated -> None (nothing to write).
        pytest.param([{"type": "special", "kind": "turn_started"}], None, id="started_without_id"),
    ],
)
def test_current_open_turn_id(events: list[dict[str, Any]], expected: str | None) -> None:
    assert current_open_turn_id(events) == expected


@pytest.mark.parametrize(
    "events, expected",
    [
        pytest.param([], False, id="empty_is_not_open"),
        pytest.param([{"type": "special", "kind": "turn_started"}], True, id="started_is_open"),
        pytest.param([{"type": "special", "kind": "turn_started"}, {"type": "special", "kind": "turn_completed"}], False, id="completed_is_closed"),
        pytest.param([{"type": "special", "kind": "turn_started"}, {"type": "special", "kind": "turn_aborted"}], False, id="aborted_is_closed"),
        # A turn mid-flight: started, then non-boundary events -> still open.
        pytest.param(
            [{"type": "special", "kind": "turn_started"}, {"type": "assistant_message"}, {"type": "tool_result"}],
            True,
            id="mid_turn_still_open",
        ),
        # A second turn started after a completed one -> open again.
        pytest.param(
            [{"type": "special", "kind": "turn_started"}, {"type": "special", "kind": "turn_completed"}, {"type": "special", "kind": "turn_started"}],
            True,
            id="new_turn_reopens",
        ),
        pytest.param([{"type": "assistant_message"}], False, id="no_markers_is_not_open"),
    ],
)
def test_codex_turn_open(events: list[dict[str, Any]], expected: bool) -> None:
    assert turn_open(events) is expected


@pytest.mark.parametrize(
    "turn_open, has_pending_tool_use, expected",
    [
        pytest.param(False, False, ActivityState.IDLE, id="closed_is_idle"),
        pytest.param(False, True, ActivityState.IDLE, id="closed_is_idle_even_with_dangling_tool"),
        pytest.param(True, False, ActivityState.THINKING, id="open_no_tool_is_thinking"),
        pytest.param(True, True, ActivityState.TOOL_RUNNING, id="open_with_tool_is_running"),
    ],
)
def test_derive_codex(turn_open: bool, has_pending_tool_use: bool, expected: ActivityState) -> None:
    assert derive(turn_open=turn_open, has_pending_tool_use=has_pending_tool_use) == expected


def test_derive_codex_stale_tail_overrides_to_idle() -> None:
    """A task_started abandoned by a prior process (tail older than process start) reads IDLE."""
    state = derive(
        turn_open=True,
        has_pending_tool_use=True,
        tail_event_at=100.0,
        process_started_at=200.0,
    )
    assert state == ActivityState.IDLE


def test_derive_codex_fresh_open_turn_reports_working() -> None:
    state = derive(
        turn_open=True,
        has_pending_tool_use=False,
        tail_event_at=300.0,
        process_started_at=200.0,
    )
    assert state == ActivityState.THINKING
