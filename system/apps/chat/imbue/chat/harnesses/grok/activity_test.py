"""The grok activity dot over grok's own session logs, replayed record by record through the session watcher.

The replay keeps the ``active`` marker as mngr's grok hooks do for a top-level turn: on from the prompt's first record
until its session logs the turn's end. The lifecycle reads WAITING, so the marker decides whether a turn runs.
"""

import heapq
import json
from operator import itemgetter
from pathlib import Path
from typing import Any

import pytest

from imbue.chat.activity_state import ACTIVE_MARKER_FILENAME
from imbue.chat.activity_state import ActivityState
from imbue.chat.activity_state import parse_iso_timestamp_to_epoch
from imbue.chat.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.chat.harnesses.events import SpecialEventKind
from imbue.chat.harnesses.grok.activity import GrokActivityTracker
from imbue.chat.harnesses.grok.session_parser import record_timestamp
from imbue.chat.harnesses.grok.testing import add_grok_session
from imbue.chat.harnesses.grok.testing import append_grok_updates
from imbue.chat.harnesses.grok.testing import find_fixture_scenario
from imbue.chat.harnesses.grok.testing import make_grok_agent_info
from imbue.chat.harnesses.grok.testing import read_fixture_root_ids
from imbue.chat.harnesses.grok.testing import read_fixture_session_ids
from imbue.chat.harnesses.grok.testing import read_fixture_updates
from imbue.chat.harnesses.grok.watcher import GrokSessionWatcher
from imbue.mngr_grok.grok_config import PROCESS_STARTED_MARKER_FILENAME
from imbue.mngr_grok.resources.grok_hook import ACTIVE_MARKER_FILENAME as MNGR_GROK_ACTIVE_MARKER_FILENAME

_ROOT = "root"
_CHILD = "child"
_WAITING = "WAITING"
# Records that move nothing a user sees.
_UNSHOWN_KINDS = frozenset({"hook_execution"})

_IDLE = ActivityState.IDLE
_THINKING = ActivityState.THINKING
_TOOL_RUNNING = ActivityState.TOOL_RUNNING


def _replay(agent_state_dir: Path, scenario: str) -> list[tuple[str, str, ActivityState]]:
    """Write a scenario's records in the order grok wrote them, reading the dot after each one.

    Each step is (whose session, the record's kind, the dot).
    """
    scenario_dir = find_fixture_scenario(scenario)
    root_ids = read_fixture_root_ids(scenario_dir)
    lines_by_session: list[list[tuple[str, str, bytes]]] = []
    for session_id in read_fixture_session_ids(scenario_dir):
        add_grok_session(agent_state_dir, session_id, b"", is_root=session_id in root_ids)
        lines = read_fixture_updates(scenario_dir, session_id).splitlines(keepends=True)
        lines_by_session.append([(record_timestamp(json.loads(line)), session_id, line) for line in lines])
    broadcast: list[dict[str, Any]] = []
    agent_info = make_grok_agent_info(agent_state_dir, "agent-grok-activity", "grok-activity")
    watcher = GrokSessionWatcher.build(agent_info, lambda _agent_id, events: broadcast.extend(events))
    tracker = GrokActivityTracker.build()
    open_root_session_ids: set[str] = set()
    steps: list[tuple[str, str, ActivityState]] = []
    for _timestamp, session_id, line in heapq.merge(*lines_by_session, key=itemgetter(0)):
        append_grok_updates(agent_state_dir, session_id, line)
        already_broadcast = len(broadcast)
        watcher._emit_cycle()
        tracker.observe(broadcast[already_broadcast:])
        kind = json.loads(line)["params"]["update"]["sessionUpdate"]
        if session_id in root_ids and kind == "user_message_chunk":
            open_root_session_ids.add(session_id)
        elif session_id in root_ids and kind == "turn_completed":
            open_root_session_ids.discard(session_id)
        else:
            pass
        if kind in _UNSHOWN_KINDS:
            continue
        state = tracker.derive(
            lifecycle_state=_WAITING, is_active_marker_present=bool(open_root_session_ids), process_started_at=None
        )
        steps.append((_ROOT if session_id in root_ids else _CHILD, kind, state))
    return steps


def test_the_tracker_reads_mngrs_shared_active_marker_and_grok_s_process_marker() -> None:
    assert GrokActivityTracker.active_marker_filename == ACTIVE_MARKER_FILENAME == MNGR_GROK_ACTIVE_MARKER_FILENAME
    assert GrokActivityTracker.marker_filename == PROCESS_STARTED_MARKER_FILENAME


def test_a_tool_turn_thinks_runs_the_tool_and_ends_idle(tmp_path: Path) -> None:
    assert _replay(tmp_path, "tool") == [
        (_ROOT, "user_message_chunk", _THINKING),
        (_ROOT, "tool_call", _TOOL_RUNNING),
        (_ROOT, "tool_call_update", _TOOL_RUNNING),
        (_ROOT, "tool_call_update", _THINKING),
        (_ROOT, "agent_message_chunk", _THINKING),
        (_ROOT, "turn_completed", _IDLE),
    ]


def test_a_cancelled_turn_ends_idle_whether_it_was_streaming_or_running_a_tool(tmp_path: Path) -> None:
    assert _replay(tmp_path, "interrupt") == [
        (_ROOT, "user_message_chunk", _THINKING),
        (_ROOT, "agent_message_chunk", _THINKING),
        (_ROOT, "turn_completed", _IDLE),
        (_ROOT, "user_message_chunk", _THINKING),
        (_ROOT, "tool_call", _TOOL_RUNNING),
        (_ROOT, "tool_call_update", _TOOL_RUNNING),
        (_ROOT, "turn_completed", _IDLE),
        (_ROOT, "user_message_chunk", _THINKING),
        (_ROOT, "agent_message_chunk", _THINKING),
        (_ROOT, "turn_completed", _IDLE),
    ]


def test_a_background_subagent_leaves_the_dot_idle_until_its_wake_turn(tmp_path: Path) -> None:
    assert _replay(tmp_path, "background_subagent") == [
        (_ROOT, "user_message_chunk", _THINKING),
        (_ROOT, "tool_call", _TOOL_RUNNING),
        (_ROOT, "tool_call_update", _TOOL_RUNNING),
        (_ROOT, "subagent_spawned", _TOOL_RUNNING),
        (_ROOT, "tool_call_update", _THINKING),
        (_ROOT, "agent_message_chunk", _THINKING),
        (_CHILD, "user_message_chunk", _THINKING),
        (_ROOT, "turn_completed", _IDLE),
        (_CHILD, "agent_message_chunk", _IDLE),
        (_CHILD, "turn_completed", _IDLE),
        (_ROOT, "subagent_finished", _IDLE),
        (_ROOT, "user_message_chunk", _THINKING),
        (_ROOT, "agent_message_chunk", _THINKING),
        (_ROOT, "turn_completed", _IDLE),
    ]


def test_a_foreground_subagent_reads_as_its_spawn_call_running_until_it_returns(tmp_path: Path) -> None:
    steps = _replay(tmp_path, "foreground_subagent_failing_hooks")
    spawned = steps.index((_ROOT, "subagent_spawned", _TOOL_RUNNING))
    returned = steps.index((_ROOT, "tool_call_update", _THINKING), spawned)
    assert {state for _session, _kind, state in steps[spawned:returned]} == {_TOOL_RUNNING}
    assert {session for session, _kind, _state in steps[spawned:returned]} == {_ROOT, _CHILD}
    assert steps[-1] == (_ROOT, "turn_completed", _IDLE)


def test_a_turn_a_quit_cut_short_ends_on_resume_and_its_killed_tool_no_longer_runs(tmp_path: Path) -> None:
    """grok closes the turn an exited process left open when ``--resume`` reloads the session."""
    assert _replay(tmp_path, "resume_after_quit") == [
        (_ROOT, "user_message_chunk", _THINKING),
        (_ROOT, "tool_call", _TOOL_RUNNING),
        (_ROOT, "tool_call_update", _TOOL_RUNNING),
        (_ROOT, "turn_completed", _IDLE),
        (_ROOT, "background_tasks", _IDLE),
        (_ROOT, "user_message_chunk", _THINKING),
        (_ROOT, "agent_message_chunk", _THINKING),
        (_ROOT, "turn_completed", _IDLE),
    ]


@pytest.mark.parametrize(
    ("lifecycle_state", "is_active_marker_present", "expected"),
    [
        pytest.param("RUNNING", False, _THINKING, id="running"),
        pytest.param(_WAITING, True, _THINKING, id="waiting-with-the-marker"),
        pytest.param(_WAITING, False, _IDLE, id="waiting"),
        pytest.param("RUNNING_BACKGROUND", False, _IDLE, id="running-background"),
        pytest.param("STOPPED", True, _IDLE, id="stopped-with-a-stale-marker"),
        pytest.param("UNKNOWN", True, _IDLE, id="unknown"),
    ],
)
def test_whether_a_turn_runs_is_mngrs_answer_whatever_the_transcript_last_said(
    lifecycle_state: str, is_active_marker_present: bool, expected: ActivityState
) -> None:
    """mngr reports the turn it is about to start for a finished background subagent before grok logs it."""
    tracker = GrokActivityTracker.build()
    tracker.observe(
        [_turn_marker(SpecialEventKind.TURN_STARTED, "2026-10-22T10:00:00Z"), _user_event("2026-10-22T10:00:00Z")]
    )
    tracker.observe([_turn_marker(SpecialEventKind.TURN_COMPLETED, "2026-10-22T10:00:05Z")])

    state = tracker.derive(
        lifecycle_state=lifecycle_state, is_active_marker_present=is_active_marker_present, process_started_at=None
    )

    assert state is expected


def test_a_subagents_unanswered_tool_call_leaves_a_top_level_turn_thinking() -> None:
    tracker = GrokActivityTracker.build()
    tracker.observe(
        [
            _turn_marker(SpecialEventKind.TURN_STARTED, "2026-10-22T10:00:00Z"),
            _tool_call_event(_CHILD, "child-call", "2026-10-22T10:00:01Z"),
        ]
    )
    assert tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=True, process_started_at=None) is (
        _THINKING
    )

    tracker.observe([_tool_call_event(_ROOT, "root-call", "2026-10-22T10:00:02Z")])
    assert tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=True, process_started_at=None) is (
        _TOOL_RUNNING
    )


def test_a_call_a_dead_process_left_unanswered_does_not_run_in_the_next_processs_turn() -> None:
    tracker = GrokActivityTracker.build()
    tracker.observe(
        [
            _turn_marker(SpecialEventKind.TURN_STARTED, "2026-10-22T10:00:00Z"),
            _tool_call_event(_ROOT, "killed-call", "2026-10-22T10:00:01Z"),
            _turn_marker(SpecialEventKind.TURN_STARTED, "2026-10-22T11:00:00Z"),
            _user_event("2026-10-22T11:00:00Z"),
        ]
    )
    relaunched_at = parse_iso_timestamp_to_epoch("2026-10-22T10:30:00Z")

    state = tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=True, process_started_at=relaunched_at)

    assert state is _THINKING


def _turn_marker(kind: SpecialEventKind, timestamp: str) -> dict[str, Any]:
    return {"type": SPECIAL_EVENT_TYPE, "kind": kind.value, "session_id": _ROOT, "timestamp": timestamp}


def _user_event(timestamp: str) -> dict[str, Any]:
    return {"type": "user_message", "session_id": _ROOT, "timestamp": timestamp, "content": "hello"}


def _tool_call_event(session_id: str, tool_call_id: str, timestamp: str) -> dict[str, Any]:
    return {
        "type": "assistant_message",
        "session_id": session_id,
        "timestamp": timestamp,
        "tool_calls": [{"tool_call_id": tool_call_id}],
    }
