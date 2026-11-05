import re
from pathlib import Path
from typing import Any

import pytest

from imbue.chat.activity_state import ActivityState
from imbue.chat.activity_state import BACKGROUND_LIFECYCLE_STATE
from imbue.chat.activity_state import RUNNING_LIFECYCLE_STATES
from imbue.chat.activity_state import WAITING_LIFECYCLE_STATE
from imbue.chat.activity_state import is_lifecycle_dead
from imbue.chat.activity_state import is_non_turn_tail_event
from imbue.chat.activity_state import is_transcript_tail_stale
from imbue.chat.activity_state import is_working_activity_state
from imbue.chat.activity_state import parse_iso_timestamp_to_epoch
from imbue.chat.activity_state import resolve_is_agent_running

_FRONTEND_SRC = Path(__file__).parent.parent.parent / "frontend" / "src"


def test_an_agent_carrying_work_past_its_turn_is_alive_but_not_running() -> None:
    """The two questions this module answers come apart exactly here.

    Its turn has ended, so no turn is in flight; work it declared will start the next one, so it
    is not dead. What acts on death restarts the agent, so reading it as dead acts against a
    live, working agent.
    """
    assert is_lifecycle_dead(BACKGROUND_LIFECYCLE_STATE) is False
    assert resolve_is_agent_running(BACKGROUND_LIFECYCLE_STATE, is_active_marker_present=False) is False
    assert BACKGROUND_LIFECYCLE_STATE not in RUNNING_LIFECYCLE_STATES


@pytest.mark.frontend
def test_the_frontends_alive_states_match_this_modules() -> None:
    """The frontend keeps its own copy of the live set, in a language that cannot import this one.

    Its ``agentLiveness.ts`` mirrors ``is_lifecycle_dead``, and the two drifting is invisible
    until a user sees a live agent's terminal unmount. Nothing but this test holds them together.
    """
    source = (_FRONTEND_SRC / "views" / "agentLiveness.ts").read_text()
    declaration = re.search(r"ALIVE_STATES: ReadonlySet<string> = new Set\(\[(.*?)\]\)", source, re.S)
    assert declaration is not None, "ALIVE_STATES is not declared the way this test reads it"
    frontend_states = set(re.findall(r'"([A-Z_]+)"', declaration.group(1)))

    assert frontend_states == set(RUNNING_LIFECYCLE_STATES) | {WAITING_LIFECYCLE_STATE, BACKGROUND_LIFECYCLE_STATE}


def test_resolve_is_agent_running_running_state_is_authoritative() -> None:
    # A reported RUNNING state means running regardless of the marker.
    assert resolve_is_agent_running("RUNNING", is_active_marker_present=False) is True
    assert resolve_is_agent_running("RUNNING_UNKNOWN_AGENT_TYPE", is_active_marker_present=False) is True


def test_resolve_is_agent_running_waiting_trusts_the_marker() -> None:
    # The lag case: a short turn leaves the reported state at WAITING while the marker flips.
    assert resolve_is_agent_running("WAITING", is_active_marker_present=True) is True
    assert resolve_is_agent_running("WAITING", is_active_marker_present=False) is False


def test_resolve_is_agent_running_terminal_state_ignores_a_stale_marker() -> None:
    # A hard-crashed agent can leave a stale marker; a STOPPED/EXITED state must still read
    # as not running so it never shows "Thinking".
    assert resolve_is_agent_running("STOPPED", is_active_marker_present=True) is False
    assert resolve_is_agent_running("EXITED", is_active_marker_present=True) is False


@pytest.mark.parametrize(
    "event, expected",
    [
        # The parser stamps the decision (harnesses/message_display.is_non_turn_tail);
        # this layer only reads it. The content-level cases live in message_display_test.
        pytest.param({"type": "user_message", "non_turn_tail": True, "content": "/model sonnet"}, True, id="stamped"),
        pytest.param({"type": "user_message", "content": "a real question"}, False, id="unstamped"),
        pytest.param({"type": "assistant_message"}, False, id="not_a_user_message"),
    ],
)
def test_is_non_turn_tail_event(event: dict[str, Any], expected: bool) -> None:
    assert is_non_turn_tail_event(event) is expected


def test_parse_iso_timestamp_to_epoch_roundtrips() -> None:
    # The same instant expressed as Z-suffixed UTC and as an explicit offset must
    # parse to the same absolute epoch.
    assert parse_iso_timestamp_to_epoch("2026-06-08T19:42:15.191Z") == pytest.approx(
        parse_iso_timestamp_to_epoch("2026-06-08T19:42:15.191+00:00")
    )


@pytest.mark.parametrize(
    "value",
    [pytest.param(None, id="none"), pytest.param("", id="empty"), pytest.param("not-a-timestamp", id="garbage")],
)
def test_parse_iso_timestamp_to_epoch_returns_none_on_bad_input(value: str | None) -> None:
    assert parse_iso_timestamp_to_epoch(value) is None


@pytest.mark.parametrize(
    "tail_event_at, process_started_at, expected",
    [
        pytest.param(100.0, 200.0, True, id="tail_before_process_start_is_stale"),
        pytest.param(200.0, 100.0, False, id="tail_after_process_start_is_fresh"),
        pytest.param(100.0, 100.0, False, id="tail_equal_to_process_start_is_fresh"),
        pytest.param(None, 200.0, False, id="missing_tail_is_not_stale"),
        pytest.param(100.0, None, False, id="missing_marker_is_not_stale"),
    ],
)
def test_is_transcript_tail_stale(
    tail_event_at: float | None, process_started_at: float | None, expected: bool
) -> None:
    assert is_transcript_tail_stale(tail_event_at=tail_event_at, process_started_at=process_started_at) is expected


@pytest.mark.parametrize(
    "activity_state, expected",
    [
        pytest.param(ActivityState.IDLE, False, id="idle"),
        pytest.param(ActivityState.THINKING, True, id="thinking"),
        pytest.param(ActivityState.TOOL_RUNNING, True, id="tool_running"),
        pytest.param(ActivityState.COMPACTING, True, id="compacting"),
    ],
)
def test_is_working_activity_state(activity_state: ActivityState, expected: bool) -> None:
    assert is_working_activity_state(activity_state) is expected
