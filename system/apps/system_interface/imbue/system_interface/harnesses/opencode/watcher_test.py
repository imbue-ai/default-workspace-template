"""Unit tests for the opencode DB-tailing watcher.

Drives ``_consume_changes`` / ``_emit_unsent`` directly (synchronously) rather than the
background poll thread, so the cursor / settle / supersession logic is tested without timing
flakiness (mirrors the antigravity and codex watcher tests).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.opencode.db_reader import opencode_db_path
from imbue.system_interface.harnesses.opencode.testing import assistant_done_data
from imbue.system_interface.harnesses.opencode.testing import build_opencode_agent_dir
from imbue.system_interface.harnesses.opencode.testing import insert_message
from imbue.system_interface.harnesses.opencode.testing import insert_part
from imbue.system_interface.harnesses.opencode.testing import text_part_data
from imbue.system_interface.harnesses.opencode.testing import tool_part_data
from imbue.system_interface.harnesses.opencode.testing import update_message
from imbue.system_interface.harnesses.opencode.testing import update_part
from imbue.system_interface.harnesses.opencode.watcher import OpenCodeDbSessionWatcher

_ROOT = "ses_root"


def _make_watcher(tmp_path: Path) -> tuple[OpenCodeDbSessionWatcher, list[list[dict[str, Any]]]]:
    build_opencode_agent_dir(tmp_path, _ROOT)
    agent_info = AgentInfo(
        id="agent-1", name="oc-test", state="RUNNING", agent_state_dir=tmp_path, claude_config_dir=tmp_path
    )
    broadcasts: list[list[dict[str, Any]]] = []
    watcher = OpenCodeDbSessionWatcher.build(agent_info, lambda _agent_id, events: broadcasts.append(events))
    return watcher, broadcasts


def _consume(watcher: OpenCodeDbSessionWatcher) -> None:
    with watcher._lock:
        watcher._consume_changes()


def _db(tmp_path: Path) -> Path:
    return opencode_db_path(tmp_path)


def test_reads_a_simple_turn(tmp_path: Path) -> None:
    watcher, _ = _make_watcher(tmp_path)
    db = _db(tmp_path)
    insert_message(db, message_id="msg_u", session_id=_ROOT, role="user", time_created=100)
    insert_part(db, part_id="prt_u", message_id="msg_u", session_id=_ROOT, time_created=100, data=text_part_data("hi"))
    insert_message(db, message_id="msg_a", session_id=_ROOT, role="assistant", time_created=200, time_updated=200)
    insert_part(
        db,
        part_id="prt_a",
        message_id="msg_a",
        session_id=_ROOT,
        time_created=200,
        time_updated=200,
        data=text_part_data("hello"),
    )
    update_message(
        db,
        message_id="msg_a",
        time_updated=210,
        data_extra=assistant_done_data(provider_id="opencode", model_id="m", finish="stop", completed=210),
    )

    _consume(watcher)
    events = watcher.get_all_events()
    assert [e["type"] for e in events] == ["user_message", "assistant_message"]
    assert events[0]["content"] == "hi"
    assert events[1]["text"] == "hello"


def test_streaming_tool_supersedes_then_result_appears(tmp_path: Path) -> None:
    watcher, broadcasts = _make_watcher(tmp_path)
    db = _db(tmp_path)
    insert_message(db, message_id="msg_a", session_id=_ROOT, role="assistant", time_created=100, time_updated=100)
    insert_part(
        db,
        part_id="prt_t",
        message_id="msg_a",
        session_id=_ROOT,
        time_created=100,
        time_updated=100,
        data=tool_part_data(tool="bash", call_id="c1", status="running", tool_input={"command": "ls"}),
    )
    # First emit: assistant_message with a tool_call, NO tool_result (running).
    watcher._emit_unsent()
    first = watcher.get_all_events()
    assert [e["type"] for e in first] == ["assistant_message"]
    assert first[0]["tool_calls"][0]["caption_label"] == "Running ls"

    # Tool completes + message settles.
    update_part(
        db,
        part_id="prt_t",
        time_updated=200,
        data=tool_part_data(tool="bash", call_id="c1", status="completed", tool_input={"command": "ls"}, output="a\n"),
    )
    update_message(
        db,
        message_id="msg_a",
        time_updated=210,
        data_extra=assistant_done_data(provider_id="p", model_id="m", finish="stop", completed=210),
    )
    watcher._emit_unsent()

    types = [e["type"] for e in watcher.get_all_events()]
    assert types == ["assistant_message", "tool_result"]
    # The second broadcast carried the tool_result AND the superseded assistant_message (its
    # tool_call state changed), so the client can upgrade its held copy in place.
    last_broadcast_types = {e["type"] for e in broadcasts[-1]}
    assert "tool_result" in last_broadcast_types
    assert "assistant_message" in last_broadcast_types


def test_cursor_advances_past_settled_and_picks_up_new_turn(tmp_path: Path) -> None:
    watcher, _ = _make_watcher(tmp_path)
    db = _db(tmp_path)
    insert_message(db, message_id="msg_u", session_id=_ROOT, role="user", time_created=100, time_updated=100)
    insert_part(
        db,
        part_id="prt_u",
        message_id="msg_u",
        session_id=_ROOT,
        time_created=100,
        time_updated=100,
        data=text_part_data("first"),
    )
    _consume(watcher)
    assert watcher.get_total_event_count() == 1
    cursor_after_first = watcher._updated_cursor

    # A brand-new later turn is picked up.
    insert_message(db, message_id="msg_u2", session_id=_ROOT, role="user", time_created=300, time_updated=300)
    insert_part(
        db,
        part_id="prt_u2",
        message_id="msg_u2",
        session_id=_ROOT,
        time_created=300,
        time_updated=300,
        data=text_part_data("second"),
    )
    _consume(watcher)
    contents = [e["content"] for e in watcher.get_all_events()]
    assert contents == ["first", "second"]
    assert watcher._updated_cursor >= cursor_after_first


def test_reconsume_of_unchanged_rows_is_a_noop(tmp_path: Path) -> None:
    watcher, _ = _make_watcher(tmp_path)
    db = _db(tmp_path)
    insert_message(db, message_id="msg_u", session_id=_ROOT, role="user", time_created=100, time_updated=100)
    insert_part(
        db,
        part_id="prt_u",
        message_id="msg_u",
        session_id=_ROOT,
        time_created=100,
        time_updated=100,
        data=text_part_data("hi"),
    )
    _consume(watcher)
    _consume(watcher)
    _consume(watcher)
    # Dedup by stable event_id: re-reading the settled boundary row adds nothing.
    assert watcher.get_total_event_count() == 1


def test_no_root_marker_yields_nothing(tmp_path: Path) -> None:
    # A state dir without the root-session marker (server not up yet).
    agent_info = AgentInfo(id="a", name="n", state="RUNNING", agent_state_dir=tmp_path, claude_config_dir=tmp_path)
    watcher = OpenCodeDbSessionWatcher.build(agent_info, lambda _a, _e: None)
    _consume(watcher)
    assert watcher.get_total_event_count() == 0
