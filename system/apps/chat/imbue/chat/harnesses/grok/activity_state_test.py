import pytest

from imbue.chat.activity_state import ActivityState
from imbue.chat.harnesses.grok.activity_state import derive

# A grok process launched at 2026-10-20T01:30:00Z, and events from before and after it.
_PROCESS_STARTED_AT = 1792459800.0
_BEFORE_LAUNCH = _PROCESS_STARTED_AT - 600.0
_AFTER_LAUNCH = _PROCESS_STARTED_AT + 5.0


@pytest.mark.parametrize(
    ("open_turn_started_ats", "pending_tool_call_ats", "expected"),
    [
        pytest.param((), (), ActivityState.IDLE, id="no-open-turn"),
        pytest.param((), (_AFTER_LAUNCH,), ActivityState.IDLE, id="an-unanswered-call-without-a-turn"),
        pytest.param((_AFTER_LAUNCH,), (), ActivityState.THINKING, id="open-turn"),
        pytest.param((_AFTER_LAUNCH,), (_AFTER_LAUNCH,), ActivityState.TOOL_RUNNING, id="open-turn-running-a-tool"),
        pytest.param((None,), (None,), ActivityState.TOOL_RUNNING, id="untimed-turn-and-call"),
        pytest.param((_BEFORE_LAUNCH,), (_AFTER_LAUNCH,), ActivityState.IDLE, id="only-a-dead-processs-turn"),
        pytest.param((_BEFORE_LAUNCH, _AFTER_LAUNCH), (), ActivityState.THINKING, id="a-live-turn-beside-a-dead-one"),
        pytest.param(
            (_AFTER_LAUNCH,), (_BEFORE_LAUNCH,), ActivityState.THINKING, id="a-dead-processs-call-left-unanswered"
        ),
    ],
)
def test_derive_reads_only_what_the_current_process_began(
    open_turn_started_ats: tuple[float | None, ...],
    pending_tool_call_ats: tuple[float | None, ...],
    expected: ActivityState,
) -> None:
    state = derive(
        open_turn_started_ats=open_turn_started_ats,
        pending_tool_call_ats=pending_tool_call_ats,
        tail_event_at=_AFTER_LAUNCH,
        process_started_at=_PROCESS_STARTED_AT,
    )
    assert state == expected


def test_derive_counts_every_open_turn_and_call_when_no_launch_is_recorded() -> None:
    state = derive(
        open_turn_started_ats=(_BEFORE_LAUNCH,), pending_tool_call_ats=(_BEFORE_LAUNCH,), process_started_at=None
    )
    assert state == ActivityState.TOOL_RUNNING


def test_a_tail_older_than_the_process_reads_idle_even_with_a_live_looking_turn() -> None:
    state = derive(
        open_turn_started_ats=(None,),
        pending_tool_call_ats=(None,),
        tail_event_at=_BEFORE_LAUNCH,
        process_started_at=_PROCESS_STARTED_AT,
    )
    assert state == ActivityState.IDLE
