"""Tests for :class:`pi_coding.watcher.PiSessionWatcher` -- tailing pi's native session
file (followed via the marker) and populating the queue from the inbox. Driven through
the read API (which refreshes from disk) rather than the background thread, so the tests
are deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.pi_coding.watcher import PiSessionWatcher


def _agent_info(state_dir: Path) -> AgentInfo:
    return AgentInfo(
        id="agent-test",
        name="test-pi",
        state="RUNNING",
        agent_state_dir=state_dir,
        claude_config_dir=state_dir / "unused",
        harness=HarnessType.PI_CODING,
    )


def _message_record(record_id: str, message: dict[str, Any]) -> dict:
    return {
        "type": "message",
        "id": record_id,
        "parentId": None,
        "timestamp": "2026-08-07T11:50:00.000Z",
        "message": message,
    }


def _write_session(session_file: Path, records: list[dict]) -> None:
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("".join(json.dumps(record) + "\n" for record in records))


def _append_session(session_file: Path, records: list[dict]) -> None:
    with session_file.open("a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _point_marker(state_dir: Path, session_file: Path) -> None:
    (state_dir / "pi_session_file").write_text(str(session_file))


def _build(state_dir: Path) -> PiSessionWatcher:
    return PiSessionWatcher.build(_agent_info(state_dir), lambda agent_id, events: None)


def _user(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def test_tails_records_and_appends(tmp_path: Path) -> None:
    session = tmp_path / "plugin" / "pi_coding" / "sessions" / "cwd" / "s.jsonl"
    _write_session(
        session,
        [
            _message_record("a", _user("hi")),
            _message_record("b", {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}),
        ],
    )
    _point_marker(tmp_path, session)
    watcher = _build(tmp_path)
    assert watcher.get_total_event_count() == 2

    _append_session(session, [_message_record("c", _user("again"))])
    events = watcher.get_all_events()
    assert [event["event_id"] for event in events] == ["pi-a", "pi-b", "pi-c"]


def test_reserialised_record_dedups_by_id(tmp_path: Path) -> None:
    session = tmp_path / "s.jsonl"
    _write_session(session, [_message_record("a", _user("hi"))])
    _point_marker(tmp_path, session)
    watcher = _build(tmp_path)
    assert watcher.get_total_event_count() == 1
    # The same record id re-appended must not double the event.
    _append_session(session, [_message_record("a", _user("hi"))])
    assert watcher.get_total_event_count() == 1


def test_rotation_to_new_file_keeps_events_and_clears_queue(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    _write_session(first, [_message_record("a", _user("one"))])
    _point_marker(tmp_path, first)
    (tmp_path / "pi_inbox").write_text(json.dumps("one") + "\n" + json.dumps("parked") + "\n")
    watcher = _build(tmp_path)
    # "one" drained (it is a user record); "parked" is still queued.
    assert [entry["content"] for entry in watcher.get_queued_messages()] == ["parked"]

    second = tmp_path / "second.jsonl"
    _write_session(second, [_message_record("z", _user("fresh"))])
    _point_marker(tmp_path, second)
    events = watcher.get_all_events()
    # Accumulated transcript survives the rotation...
    assert {event["event_id"] for event in events} == {"pi-a", "pi-z"}
    # ...but the queue is reset (pi's followUp queue does not survive /new).
    assert watcher.get_queued_messages() == []


def test_queue_enqueues_from_inbox_and_leaves_on_drained_user_turn(tmp_path: Path) -> None:
    session = tmp_path / "s.jsonl"
    _write_session(session, [])
    _point_marker(tmp_path, session)
    inbox = tmp_path / "pi_inbox"
    inbox.write_text(json.dumps("please do X") + "\n")
    watcher = _build(tmp_path)
    assert [entry["content"] for entry in watcher.get_queued_messages()] == ["please do X"]

    # The message drains into the transcript as a user turn -> it leaves the queue.
    _append_session(session, [_message_record("u", _user("please do X"))])
    assert watcher.get_queued_messages() == []


def test_notify_idle_clears_the_queue(tmp_path: Path) -> None:
    session = tmp_path / "s.jsonl"
    _write_session(session, [])
    _point_marker(tmp_path, session)
    (tmp_path / "pi_inbox").write_text(json.dumps("stuck") + "\n")
    watcher = _build(tmp_path)
    assert len(watcher.get_queued_messages()) == 1
    assert watcher.notify_idle() == []
    assert watcher.get_queued_messages() == []


def test_no_session_yet_reads_empty(tmp_path: Path) -> None:
    watcher = _build(tmp_path)
    assert watcher.get_all_events() == []
    assert watcher.get_queued_messages() == []


def _debounce_watcher(tmp_path: Path, clock: list[float]) -> tuple[PiSessionWatcher, list[list[str]]]:
    session = tmp_path / "s.jsonl"
    _write_session(session, [])
    _point_marker(tmp_path, session)
    watcher = _build(tmp_path)
    pushes: list[list[str]] = []
    watcher.set_queue_snapshot_callback(lambda snapshot: pushes.append([entry["content"] for entry in snapshot]))
    watcher._now = lambda: clock[0]
    return watcher, pushes


def test_queue_snapshot_debounced_transient_is_never_pushed(tmp_path: Path) -> None:
    # A message sent to an idle agent lands in the inbox then drains into a real turn within
    # the debounce window; it must never be pushed as "queued".
    clock = [100.0]
    watcher, pushes = _debounce_watcher(tmp_path, clock)
    session = tmp_path / "s.jsonl"
    (tmp_path / "pi_inbox").write_text(json.dumps("quick") + "\n")
    watcher._emit_unsent()
    # within the window: not surfaced
    assert pushes == []
    clock[0] = 101.0
    watcher._emit_unsent()
    assert pushes == []
    # It drains before the window elapses.
    _append_session(session, [_message_record("u", _user("quick"))])
    clock[0] = 101.5
    watcher._emit_unsent()
    clock[0] = 104.0
    watcher._emit_unsent()
    # the transient queued entry was never shown
    assert pushes == []


def test_queue_snapshot_debounced_persistent_is_pushed(tmp_path: Path) -> None:
    # A genuinely parked message (never drains) surfaces once it outlives the window.
    clock = [200.0]
    watcher, pushes = _debounce_watcher(tmp_path, clock)
    (tmp_path / "pi_inbox").write_text(json.dumps("parked") + "\n")
    watcher._emit_unsent()
    # still within the window
    assert pushes == []
    clock[0] = 202.5
    watcher._emit_unsent()
    assert pushes == [["parked"]]
