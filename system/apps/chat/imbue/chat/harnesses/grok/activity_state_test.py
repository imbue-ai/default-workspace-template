import pytest

from imbue.chat.activity_state import ActivityState
from imbue.chat.harnesses.grok.activity_state import derive

# A grok process launched at 2026-10-20T01:30:00Z, and events from before and after it.
_PROCESS_STARTED_AT = 1792459800.0
_BEFORE_LAUNCH = _PROCESS_STARTED_AT - 600.0
_AFTER_LAUNCH = _PROCESS_STARTED_AT + 5.0


@pytest.mark.parametrize(
    ("is_turn_in_flight", "pending_tool_call_ats", "expected"),
    [
        pytest.param(False, (), ActivityState.IDLE, id="no-turn"),
        pytest.param(False, (_AFTER_LAUNCH,), ActivityState.IDLE, id="an-unanswered-call-without-a-turn"),
        pytest.param(True, (), ActivityState.THINKING, id="a-turn"),
        pytest.param(True, (_AFTER_LAUNCH,), ActivityState.TOOL_RUNNING, id="a-turn-running-a-tool"),
        pytest.param(True, (None,), ActivityState.TOOL_RUNNING, id="an-untimed-call"),
        pytest.param(True, (_BEFORE_LAUNCH,), ActivityState.THINKING, id="a-dead-processs-call-left-unanswered"),
        pytest.param(
            True, (_BEFORE_LAUNCH, _AFTER_LAUNCH), ActivityState.TOOL_RUNNING, id="a-live-call-beside-a-dead-one"
        ),
    ],
)
def test_derive_reads_the_turn_from_mngr_and_counts_only_the_current_processs_calls(
    is_turn_in_flight: bool, pending_tool_call_ats: tuple[float | None, ...], expected: ActivityState
) -> None:
    state = derive(
        is_turn_in_flight=is_turn_in_flight,
        pending_tool_call_ats=pending_tool_call_ats,
        tail_event_at=_AFTER_LAUNCH,
        process_started_at=_PROCESS_STARTED_AT,
    )
    assert state == expected


def test_derive_counts_every_unanswered_call_when_no_launch_is_recorded() -> None:
    state = derive(is_turn_in_flight=True, pending_tool_call_ats=(_BEFORE_LAUNCH,), process_started_at=None)
    assert state == ActivityState.TOOL_RUNNING


def test_a_tail_older_than_the_process_reads_idle_even_while_mngr_reports_a_turn() -> None:
    state = derive(
        is_turn_in_flight=True,
        pending_tool_call_ats=(None,),
        tail_event_at=_BEFORE_LAUNCH,
        process_started_at=_PROCESS_STARTED_AT,
    )
    assert state == ActivityState.IDLE
