"""Unit tests for the opencode SQLite read layer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from imbue.system_interface.harnesses.opencode.db_reader import OpenCodeMessage
from imbue.system_interface.harnesses.opencode.db_reader import OpenCodePart
from imbue.system_interface.harnesses.opencode.db_reader import is_message_settled
from imbue.system_interface.harnesses.opencode.db_reader import read_changed_messages
from imbue.system_interface.harnesses.opencode.db_reader import read_root_session_id
from imbue.system_interface.harnesses.opencode.testing import build_opencode_agent_dir
from imbue.system_interface.harnesses.opencode.testing import insert_message
from imbue.system_interface.harnesses.opencode.testing import insert_part
from imbue.system_interface.harnesses.opencode.testing import text_part_data

_ROOT = "ses_root"


def test_read_root_session_id(tmp_path: Path) -> None:
    build_opencode_agent_dir(tmp_path, _ROOT)
    assert read_root_session_id(tmp_path) == _ROOT


def test_missing_marker_returns_none(tmp_path: Path) -> None:
    assert read_root_session_id(tmp_path) is None


def test_missing_db_returns_empty(tmp_path: Path) -> None:
    # No db created yet (agent has not taken a turn) -> degrade to empty, not raise.
    messages, parts = read_changed_messages(tmp_path / "nope.db", _ROOT, 0)
    assert messages == [] and parts == {}


def test_reads_message_and_its_parts(tmp_path: Path) -> None:
    db = build_opencode_agent_dir(tmp_path, _ROOT)
    insert_message(db, message_id="msg_u", session_id=_ROOT, role="user", time_created=100)
    insert_part(db, part_id="prt_u", message_id="msg_u", session_id=_ROOT, time_created=100, data=text_part_data("hi"))
    messages, parts = read_changed_messages(db, _ROOT, 0)
    assert [m.id for m in messages] == ["msg_u"]
    assert [p.text for p in parts["msg_u"]] == ["hi"]


def test_filters_to_root_session(tmp_path: Path) -> None:
    db = build_opencode_agent_dir(tmp_path, _ROOT)
    insert_message(db, message_id="msg_root", session_id=_ROOT, role="user", time_created=100)
    insert_message(db, message_id="msg_child", session_id="ses_child", role="user", time_created=101)
    messages, _ = read_changed_messages(db, _ROOT, 0)
    assert [m.id for m in messages] == ["msg_root"]


def test_watermark_excludes_older_but_includes_part_touch(tmp_path: Path) -> None:
    db = build_opencode_agent_dir(tmp_path, _ROOT)
    # A settled message at t=100, and a message whose PART was updated at t=300.
    insert_message(db, message_id="msg_a", session_id=_ROOT, role="user", time_created=100, time_updated=100)
    insert_part(
        db,
        part_id="prt_a",
        message_id="msg_a",
        session_id=_ROOT,
        time_created=100,
        time_updated=100,
        data=text_part_data("a"),
    )
    insert_message(db, message_id="msg_b", session_id=_ROOT, role="assistant", time_created=200, time_updated=200)
    insert_part(
        db,
        part_id="prt_b",
        message_id="msg_b",
        session_id=_ROOT,
        time_created=200,
        time_updated=300,
        data=text_part_data("b"),
    )
    # cursor at 250: msg_a (all <=100) excluded; msg_b included via its part's time_updated=300.
    messages, _ = read_changed_messages(db, _ROOT, 250)
    assert [m.id for m in messages] == ["msg_b"]


def test_settle_rules() -> None:
    user = _msg("user")
    assert is_message_settled(user, [])
    streaming = _msg("assistant", completed=None)
    assert not is_message_settled(streaming, [])
    done_no_tools = _msg("assistant", completed=1)
    assert is_message_settled(done_no_tools, [])
    tool_running = _part_tool("running")
    assert not is_message_settled(_msg("assistant", completed=1), [tool_running])
    tool_done = _part_tool("completed")
    assert is_message_settled(_msg("assistant", completed=1), [tool_done])


def test_malformed_data_row_is_skipped_not_raised(tmp_path: Path) -> None:
    db = build_opencode_agent_dir(tmp_path, _ROOT)
    connection = sqlite3.connect(db)
    connection.execute("INSERT INTO message VALUES (?,?,?,?,?)", ("msg_bad", _ROOT, 100, 100, "{not json"))
    connection.commit()
    connection.close()
    messages, _ = read_changed_messages(db, _ROOT, 0)
    assert messages == []


# --- helpers for the pure settle test (no db) ---


def _msg(role: str, *, completed: int | None = None) -> OpenCodeMessage:
    return OpenCodeMessage(
        id="m",
        session_id="s",
        time_created=1,
        time_updated=1,
        role=role,
        provider_id=None,
        model_id=None,
        finish=None,
        completed=completed,
        tokens=None,
    )


def _part_tool(status: str) -> OpenCodePart:
    return OpenCodePart(
        id="p",
        message_id="m",
        session_id="s",
        time_created=1,
        time_updated=1,
        kind="tool",
        text="",
        synthetic=False,
        tool_name="bash",
        call_id="c",
        state_status=status,
        state_input={},
        state_output="",
        state_error="",
    )
