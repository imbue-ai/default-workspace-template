"""Tests for :class:`pi_coding.watcher.PiSessionWatcher` -- tailing pi's native session
file (followed via the marker) and populating the queue from the inbox. Driven through
the read API (which refreshes from disk) rather than the background thread, so the tests
are deterministic.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.pi_coding.inbox import PI_INTERRUPT_KEY
from imbue.system_interface.harnesses.pi_coding.inbox import PI_RETRACT_KEY
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


def _message_record(record_id: str, message: dict[str, Any], timestamp: str = "2026-08-07T11:50:00.000Z") -> dict:
    return {
        "type": "message",
        "id": record_id,
        "parentId": None,
        "timestamp": timestamp,
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


def _touch_process_started(state_dir: Path, mtime_iso: str) -> None:
    """Create the ``pi_process_started`` boundary marker with its mtime pinned to
    ``mtime_iso``, the way mngr's launch prelude touches it on every launch/resume."""
    marker = state_dir / "pi_process_started"
    marker.write_text("")
    epoch = datetime.fromisoformat(mtime_iso).timestamp()
    os.utime(marker, (epoch, epoch))


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


@pytest.mark.parametrize("sentinel_key", [PI_INTERRUPT_KEY, PI_RETRACT_KEY])
def test_sentinel_line_clears_the_tracked_queue(tmp_path: Path, sentinel_key: str) -> None:
    # A flush or retract sentinel replays as a positional clear: every message before it was
    # committed (flush) or discarded (retract) in the live session, so the mirror is empty at
    # the sentinel's position -- keeping the ledger balanced across a backend restart.
    session = tmp_path / "s.jsonl"
    _write_session(session, [])
    _point_marker(tmp_path, session)
    inbox = tmp_path / "pi_inbox"
    inbox.write_text(
        json.dumps("first") + "\n" + json.dumps("second") + "\n" + json.dumps({sentinel_key: True}) + "\n"
    )
    watcher = _build(tmp_path)
    assert watcher.get_queued_messages() == []


@pytest.mark.parametrize("sentinel_key", [PI_INTERRUPT_KEY, PI_RETRACT_KEY])
def test_strings_after_a_sentinel_re_enqueue(tmp_path: Path, sentinel_key: str) -> None:
    # A message sent after the tap/stop enqueues normally: the sentinel cleared the prior set,
    # and the following string is a fresh queued entry.
    session = tmp_path / "s.jsonl"
    _write_session(session, [])
    _point_marker(tmp_path, session)
    inbox = tmp_path / "pi_inbox"
    inbox.write_text(json.dumps("stale") + "\n" + json.dumps({sentinel_key: True}) + "\n")
    watcher = _build(tmp_path)
    assert watcher.get_queued_messages() == []

    with inbox.open("a") as handle:
        handle.write(json.dumps("brand new") + "\n")
    assert [entry["content"] for entry in watcher.get_queued_messages()] == ["brand new"]


def test_drain_older_than_process_start_does_not_pop(tmp_path: Path) -> None:
    # A user turn replayed from a dead process generation (its timestamp predates the
    # pi_process_started marker) must not eat a current-generation queued entry.
    session = tmp_path / "s.jsonl"
    _write_session(session, [])
    _point_marker(tmp_path, session)
    _touch_process_started(tmp_path, "2026-08-07T12:00:00+00:00")
    (tmp_path / "pi_inbox").write_text(json.dumps("parked now") + "\n")
    watcher = _build(tmp_path)
    assert [entry["content"] for entry in watcher.get_queued_messages()] == ["parked now"]

    _append_session(session, [_message_record("old", _user("drained long ago"), timestamp="2026-08-07T11:50:00.000Z")])
    assert [entry["content"] for entry in watcher.get_queued_messages()] == ["parked now"]


def test_drain_at_or_after_process_start_pops(tmp_path: Path) -> None:
    # A current-generation drain (timestamp >= the marker mtime) pops as before.
    session = tmp_path / "s.jsonl"
    _write_session(session, [])
    _point_marker(tmp_path, session)
    _touch_process_started(tmp_path, "2026-08-07T12:00:00+00:00")
    (tmp_path / "pi_inbox").write_text(json.dumps("please do X") + "\n")
    watcher = _build(tmp_path)
    assert [entry["content"] for entry in watcher.get_queued_messages()] == ["please do X"]

    _append_session(session, [_message_record("u", _user("please do X"), timestamp="2026-08-07T12:10:00.000Z")])
    assert watcher.get_queued_messages() == []


def test_missing_process_start_marker_pops(tmp_path: Path) -> None:
    # No marker on disk -> every drain pops (today's behavior; over-popping errs
    # toward an empty mirror, the contract-safe direction).
    session = tmp_path / "s.jsonl"
    _write_session(session, [])
    _point_marker(tmp_path, session)
    (tmp_path / "pi_inbox").write_text(json.dumps("queued") + "\n")
    watcher = _build(tmp_path)
    assert [entry["content"] for entry in watcher.get_queued_messages()] == ["queued"]

    _append_session(session, [_message_record("u", _user("queued"), timestamp="2020-01-01T00:00:00.000Z")])
    assert watcher.get_queued_messages() == []


def test_unparseable_drain_timestamp_pops(tmp_path: Path) -> None:
    # A drain whose timestamp cannot be parsed also pops (the contract-safe direction),
    # even when the marker exists.
    session = tmp_path / "s.jsonl"
    _write_session(session, [])
    _point_marker(tmp_path, session)
    _touch_process_started(tmp_path, "2026-08-07T12:00:00+00:00")
    (tmp_path / "pi_inbox").write_text(json.dumps("queued") + "\n")
    watcher = _build(tmp_path)
    assert [entry["content"] for entry in watcher.get_queued_messages()] == ["queued"]

    _append_session(session, [_message_record("u", _user("queued"), timestamp="not-a-timestamp")])
    assert watcher.get_queued_messages() == []


def test_truncation_then_append_replays_only_appended_lines(tmp_path: Path) -> None:
    # The extension archives-and-truncates pi_inbox at load; the watcher's existing
    # shrink-reset must then replay exactly the newly-appended lines, with queued ids
    # re-based from index 0 (identical to a from-scratch replay of the same file).
    session = tmp_path / "s.jsonl"
    _write_session(session, [])
    _point_marker(tmp_path, session)
    inbox = tmp_path / "pi_inbox"
    inbox.write_text(json.dumps("stale one") + "\n" + json.dumps("stale two") + "\n")
    watcher = _build(tmp_path)
    assert [entry["content"] for entry in watcher.get_queued_messages()] == ["stale one", "stale two"]

    inbox.write_text(json.dumps("fresh") + "\n")
    snapshot = watcher.get_queued_messages()
    assert [entry["content"] for entry in snapshot] == ["fresh"]
    # Ids are re-based from 0: the surviving watcher agrees with a fresh replay.
    assert snapshot == _build(tmp_path).get_queued_messages()


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


def _snapshot_watcher(tmp_path: Path) -> tuple[PiSessionWatcher, list[list[str]]]:
    session = tmp_path / "s.jsonl"
    _write_session(session, [])
    _point_marker(tmp_path, session)
    watcher = _build(tmp_path)
    pushes: list[list[str]] = []
    watcher.set_queue_snapshot_callback(lambda snapshot: pushes.append([entry["content"] for entry in snapshot]))
    return watcher, pushes


def test_queue_snapshot_parked_message_surfaces_immediately(tmp_path: Path) -> None:
    # No debounce: a genuinely parked message (never drains) surfaces on the very first emit,
    # not after a stability window (contract A3/A3b: the UI queue IS the harness queue).
    watcher, pushes = _snapshot_watcher(tmp_path)
    (tmp_path / "pi_inbox").write_text(json.dumps("parked") + "\n")
    watcher._emit_cycle()
    assert pushes == [["parked"]]


def test_queue_snapshot_transient_shows_then_clears(tmp_path: Path) -> None:
    # A message sent to an idle agent that lands in the inbox and then drains into a real turn
    # in a LATER cycle surfaces as a chip and then clears -- faithful to pi's real inbox state.
    # (When the enqueue and drain land in the SAME refresh they net out and no chip shows; this
    # exercises the separate-cycle case.)
    watcher, pushes = _snapshot_watcher(tmp_path)
    session = tmp_path / "s.jsonl"
    (tmp_path / "pi_inbox").write_text(json.dumps("quick") + "\n")
    watcher._emit_cycle()
    assert pushes == [["quick"]]
    # It drains on a later cycle -> the chip is removed (the queue empties).
    _append_session(session, [_message_record("u", _user("quick"))])
    watcher._emit_cycle()
    assert pushes == [["quick"], []]


def _assistant(text: str) -> dict:
    return {"role": "assistant", "model": "m", "content": [{"type": "text", "text": text}]}


def test_pre_rotation_session_files_are_recovered_from_disk(tmp_path: Path) -> None:
    """Cross-rotation history: files a ``/new`` left behind are registered chronologically
    ahead of the live file, so a rebuilt watcher (backend restart, eviction) recovers the
    whole timeline instead of only the post-rotation slice."""
    sessions = tmp_path / "plugin" / "pi_coding" / "sessions" / "cwd"
    old_a = sessions / "20260101_aaa.jsonl"
    old_b = sessions / "20260102_bbb.jsonl"
    live = sessions / "20260103_ccc.jsonl"
    _write_session(old_a, [_message_record("a1", _user("first era"))])
    _write_session(old_b, [_message_record("b1", _user("second era"))])
    _write_session(live, [_message_record("c1", _user("current era"))])
    # Distinct mtimes, oldest first, so the chronological order is unambiguous.
    for index, path in enumerate((old_a, old_b, live)):
        os.utime(path, (1_700_000_000 + index * 1000, 1_700_000_000 + index * 1000))
    _point_marker(tmp_path, live)

    watcher = _build(tmp_path)
    contents = [e["content"] for e in watcher.get_all_events()]
    assert contents == ["first era", "second era", "current era"]

    # The live file keeps tailing after the static backfill.
    _append_session(live, [_message_record("c2", _user("still going"))])
    contents = [e["content"] for e in watcher.get_all_events()]
    assert contents == ["first era", "second era", "current era", "still going"]


def test_static_file_with_unflushed_trailing_line_is_retried_not_lost(tmp_path: Path) -> None:
    """A non-live file swept the instant it stops being live but before its last write
    finishes flushing must not be marked fully consumed: the partial line is retried on a
    later cycle instead of being permanently dropped.

    The recovered line can land after events the live file already contributed in the
    meantime, rather than in strict chronological position -- a flat append-list can't
    retroactively splice it back in -- but that is an accepted trade-off versus losing it
    outright, so this only asserts nothing goes missing, not exact position.
    """
    sessions = tmp_path / "plugin" / "pi_coding" / "sessions" / "cwd"
    old = sessions / "20260101_aaa.jsonl"
    live = sessions / "20260102_bbb.jsonl"
    sessions.mkdir(parents=True)
    complete_line = json.dumps(_message_record("a1", _user("first era"))) + "\n"
    second_record = _message_record("a2", _user("still writing"))
    partial_fragment = json.dumps(second_record)[:20]
    old.write_bytes((complete_line + partial_fragment).encode())
    _write_session(live, [_message_record("b1", _user("current era"))])
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(live, (1_700_001_000, 1_700_001_000))
    _point_marker(tmp_path, live)

    watcher = _build(tmp_path)
    contents = [e["content"] for e in watcher.get_all_events()]
    assert contents == ["first era", "current era"]

    # The old file was not marked consumed, so once the write finishes flushing a later
    # cycle picks up the completed line -- nothing was lost.
    old.write_bytes((complete_line + json.dumps(second_record) + "\n").encode())
    contents = [e["content"] for e in watcher.get_all_events()]
    assert len(contents) == 3
    assert set(contents) == {"first era", "still writing", "current era"}


def test_get_event_detail_serves_full_input_output_and_thinking(tmp_path: Path) -> None:
    session = tmp_path / "plugin" / "pi_coding" / "sessions" / "cwd" / "s.jsonl"
    big_command = "echo " + "x" * 4000
    _write_session(
        session,
        [
            _message_record(
                "a1",
                {
                    "role": "assistant",
                    "model": "m",
                    "content": [
                        {"type": "thinking", "thinking": "let me think"},
                        {"type": "toolCall", "id": "t1", "name": "bash", "arguments": {"command": big_command}},
                    ],
                },
            ),
            _message_record(
                "r1",
                {
                    "role": "toolResult",
                    "toolCallId": "t1",
                    "toolName": "bash",
                    "content": [{"type": "text", "text": "y" * 6000}],
                    "isError": False,
                },
            ),
        ],
    )
    _point_marker(tmp_path, session)
    watcher = _build(tmp_path)
    events = watcher.get_all_events()
    assistant = next(e for e in events if e["type"] == "assistant_message")
    result = next(e for e in events if e["type"] == "tool_result")
    assert assistant["has_thinking"] is True
    assert result["output_chars"] == 6000

    detail = watcher.get_event_detail(assistant["event_id"])
    assert detail is not None
    assert big_command in detail["inputs_by_tool_call_id"]["t1"]
    assert detail["thinking"] == "let me think"
    detail = watcher.get_event_detail(result["event_id"])
    assert detail is not None
    assert detail["output"] == "y" * 6000
