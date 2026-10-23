"""The grok activity dot over grok's own session logs, replayed record by record through the session watcher.

mngr_grok's ``active`` marker is on for every step here, as it is while a turn or a background subagent
runs; the dot must follow the transcript's top-level turns regardless.
"""

import heapq
import json
from operator import itemgetter
from pathlib import Path
from typing import Any

from imbue.chat.activity_state import ActivityState
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

_ROOT = "root"
_CHILD = "child"
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
    steps: list[tuple[str, str, ActivityState]] = []
    for _timestamp, session_id, line in heapq.merge(*lines_by_session, key=itemgetter(0)):
        append_grok_updates(agent_state_dir, session_id, line)
        already_broadcast = len(broadcast)
        watcher._emit_cycle()
        tracker.observe(broadcast[already_broadcast:])
        kind = json.loads(line)["params"]["update"]["sessionUpdate"]
        if kind in _UNSHOWN_KINDS:
            continue
        state = tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=True, process_started_at=None)
        steps.append((_ROOT if session_id in root_ids else _CHILD, kind, state))
    return steps


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


def test_a_wake_turn_grok_retries_reads_thinking_until_it_fails(tmp_path: Path) -> None:
    steps = _replay(tmp_path, "failed_wake")
    retries = [state for _session, kind, state in steps if kind == "retry_state"]
    assert len(retries) == 15
    assert set(retries) == {_THINKING}
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
