"""Tests for the mood fold over mngr's agents event file and the chats' background task markers, and the reader that
watches both."""

import json
import os
import queue
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.avatar.designs import AvatarMood
from imbue.system_interface.avatar.status import AvatarStatus
from imbue.system_interface.avatar.status import AvatarStatusReader
from imbue.system_interface.avatar.status import STALE_AFTER
from imbue.system_interface.avatar.status import _TAIL_BLOCK_BYTES
from imbue.system_interface.avatar.status import agent_events_path
from imbue.system_interface.avatar.status import fold_agent_events
from imbue.system_interface.avatar.status import parse_event_timestamp
from imbue.system_interface.avatar.status import read_avatar_status
from imbue.system_interface.avatar.status import read_tail_lines_back_to_snapshot
from imbue.system_interface.avatar.testing import exited_process_pid
from imbue.system_interface.avatar.testing import remove_background_task_marker
from imbue.system_interface.avatar.testing import write_background_task_marker
from imbue.system_interface.shell.testing import drain_messages
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _stamp(offset: timedelta = timedelta()) -> str:
    return (_NOW + offset).strftime("%Y-%m-%dT%H:%M:%S.%f000Z")


def _agent(agent_id: str, state: str, is_primary: bool = False) -> dict[str, Any]:
    labels = {"is_primary": "true"} if is_primary else {}
    return {"id": agent_id, "state": state, "labels": labels}


def _event(event_type: str, offset: timedelta = timedelta(), **fields: Any) -> str:
    return json.dumps({"timestamp": _stamp(offset), "type": event_type, "event_id": "e", "source": "mngr", **fields})


def test_the_fold_reads_the_last_snapshot_and_every_later_state_and_removal() -> None:
    lines = [
        _event("AGENTS_FULL_STATE", timedelta(minutes=-9), agents=[_agent("old", "RUNNING")]),
        _event("AGENTS_FULL_STATE", timedelta(minutes=-4), agents=[_agent("a", "STOPPED"), _agent("b", "STOPPED")]),
        _event("AGENT_STATE", timedelta(minutes=-3), agent=_agent("a", "RUNNING")),
        _event("AGENT_REMOVED", timedelta(minutes=-2), agent_id="a", agent_name="a", host_id="h"),
    ]
    assert fold_agent_events(lines, _NOW) == AvatarStatus(mood=AvatarMood.IDLE, is_stale=False)
    lines.append(_event("AGENT_STATE", timedelta(minutes=-1), agent=_agent("b", "RUNNING_UNKNOWN_AGENT_TYPE")))
    assert fold_agent_events(lines, _NOW).mood is AvatarMood.WORKING


def test_the_primary_agent_never_counts_as_working() -> None:
    lines = [_event("AGENTS_FULL_STATE", agents=[_agent("services", "RUNNING", is_primary=True)])]
    assert fold_agent_events(lines, _NOW).mood is AvatarMood.IDLE
    lines.append(_event("AGENT_STATE", agent=_agent("worker", "RUNNING")))
    assert fold_agent_events(lines, _NOW).mood is AvatarMood.WORKING


def test_a_malformed_line_is_skipped_and_the_rest_still_folds() -> None:
    lines = ["{not json", "[1, 2]", _event("AGENT_STATE", agent=_agent("a", "RUNNING")), "", '{"type": 7}']
    assert fold_agent_events(lines, _NOW) == AvatarStatus(mood=AvatarMood.WORKING, is_stale=False)


def test_no_lines_is_stale_and_idle() -> None:
    assert fold_agent_events([], _NOW) == AvatarStatus(mood=AvatarMood.IDLE, is_stale=True)


def test_the_status_goes_stale_past_twice_the_snapshot_interval() -> None:
    lines = [_event("AGENT_STATE", agent=_agent("a", "RUNNING"))]
    assert fold_agent_events(lines, _NOW + STALE_AFTER).is_stale is False
    assert fold_agent_events(lines, _NOW + STALE_AFTER + timedelta(seconds=1)).is_stale is True
    assert fold_agent_events(lines, _NOW + STALE_AFTER + timedelta(seconds=1)).mood is AvatarMood.WORKING


def test_timestamps_parse_with_nanoseconds_and_offsets() -> None:
    assert parse_event_timestamp("2026-09-20T12:00:00.123456789Z") == datetime(
        2026, 9, 20, 12, 0, 0, 123456, tzinfo=timezone.utc
    )
    assert parse_event_timestamp("2026-09-20T12:00:00+02:00") == datetime(
        2026, 9, 20, 12, 0, tzinfo=timezone(timedelta(hours=2))
    )
    assert parse_event_timestamp("yesterday") is None


def _padded_lines(line: str, total_bytes: int) -> list[str]:
    """Copies of ``line`` padded with trailing spaces (which JSON tolerates) that, each with its newline, take
    exactly ``total_bytes``."""
    padding = 200
    per_line = len(line) + padding + 1
    count, remainder = divmod(total_bytes, per_line)
    lines = [line + " " * padding] * (count - 1)
    return [*lines, line + " " * (padding + remainder)]


def test_the_tail_read_stops_at_the_last_snapshot_even_when_a_block_boundary_cuts_its_line(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    snapshot = _event("AGENTS_FULL_STATE", agents=[_agent("a", "RUNNING")])
    filler = _event("AGENT_STATE", agent=_agent("filler", "STOPPED"))
    # Sized so the block read first from the end begins a few bytes into the snapshot line: the line is cut, so
    # the read must go on to the block that holds its start rather than drop it.
    trailing = _padded_lines(filler, _TAIL_BLOCK_BYTES + 5 - (len(snapshot) + 1))
    first = _event("AGENT_STATE", agent=_agent("first", "STOPPED"))
    lines = [first, *_padded_lines(filler, 2 * _TAIL_BLOCK_BYTES), snapshot, *trailing]
    path.write_text("\n".join(lines) + "\n")
    tail = read_tail_lines_back_to_snapshot(path)
    # Not the whole file: the read ends in the block that holds the snapshot's start.
    assert first not in tail
    assert all(json.loads(line) for line in tail if line.strip())
    assert [line.strip() for line in tail[tail.index(snapshot) + 1 :] if line.strip()] == [filler] * len(trailing)
    assert read_avatar_status(path, tmp_path / "background-tasks", _NOW) == AvatarStatus(
        mood=AvatarMood.WORKING, is_stale=False
    )
    # A file with no snapshot is read whole.
    path.write_text("\n".join([filler] * 3) + "\n")
    assert read_tail_lines_back_to_snapshot(path)[:3] == [filler] * 3


def test_the_tail_read_finds_a_snapshot_marker_that_a_block_boundary_splits(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    marker = '"AGENTS_FULL_STATE"'
    old_snapshot = _event("AGENTS_FULL_STATE", agents=[_agent("old", "STOPPED")])
    snapshot = _event("AGENTS_FULL_STATE", agents=[_agent("a", "RUNNING")])
    filler = _event("AGENT_STATE", agent=_agent("filler", "STOPPED"))
    # Sized so the block read first from the end begins five bytes into the newest snapshot's type marker: the
    # marker straddles the boundary, and the read must still end in the block that holds the line's start
    # rather than go on to the older snapshot.
    into_marker = snapshot.index(marker) + 5
    trailing = _padded_lines(filler, _TAIL_BLOCK_BYTES - (len(snapshot) + 1 - into_marker))
    lines = [old_snapshot, *_padded_lines(filler, 2 * _TAIL_BLOCK_BYTES), snapshot, *trailing]
    path.write_text("\n".join(lines) + "\n")
    tail = read_tail_lines_back_to_snapshot(path)
    assert snapshot in tail
    assert old_snapshot not in tail
    assert read_avatar_status(path, tmp_path / "background-tasks", _NOW) == AvatarStatus(
        mood=AvatarMood.WORKING, is_stale=False
    )


def test_an_absent_file_reads_stale_and_idle(tmp_path: Path) -> None:
    assert read_avatar_status(tmp_path / "missing.jsonl", tmp_path / "background-tasks", _NOW) == AvatarStatus(
        mood=AvatarMood.IDLE, is_stale=True
    )


def test_a_busy_chat_keeps_the_mood_working_with_no_running_agent(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(_event("AGENTS_FULL_STATE", agents=[_agent("lead", "WAITING")]) + "\n")
    root = tmp_path / "background-tasks"
    assert read_avatar_status(path, root, _NOW) == AvatarStatus(mood=AvatarMood.IDLE, is_stale=False)
    # A marker whose process has exited will never wake its agent.
    write_background_task_marker(root, "agent-lead", "gone", exited_process_pid())
    assert read_avatar_status(path, root, _NOW).mood is AvatarMood.IDLE
    write_background_task_marker(root, "agent-lead", "poll", os.getpid())
    assert read_avatar_status(path, root, _NOW) == AvatarStatus(mood=AvatarMood.WORKING, is_stale=False)
    # The staleness is the events file's alone.
    assert read_avatar_status(path, root, _NOW + STALE_AFTER + timedelta(seconds=1)) == AvatarStatus(
        mood=AvatarMood.WORKING, is_stale=True
    )


def test_the_events_path_follows_the_host_directory_variable(tmp_path: Path) -> None:
    assert agent_events_path({"MNGR_HOST_DIR": str(tmp_path)}) == tmp_path / "events/mngr/agents/events.jsonl"
    assert agent_events_path({}) == Path.home() / ".mngr/events/mngr/agents/events.jsonl"


def _live_event(agent_id: str, state: str) -> str:
    """One agent state line stamped now, so the fold reads it as fresh."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({"timestamp": now, "type": "AGENT_STATE", "agent": _agent(agent_id, state)}) + "\n"


def test_the_reader_refolds_a_write_through_the_file_watch(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(_live_event("a", "STOPPED"))
    broadcaster = WebSocketBroadcaster()
    window: queue.Queue[str | None] = broadcaster.register()
    # The periodic re-check is far off: only the watch can wake the loop for the writes below.
    reader = AvatarStatusReader(
        events_path=path,
        background_tasks_root=tmp_path / "background-tasks",
        broadcaster=broadcaster,
        debounce_seconds=0.02,
        stale_check_interval_seconds=60.0,
    )
    reader.start()
    try:
        assert reader.current() == AvatarStatus(mood=AvatarMood.IDLE, is_stale=False)
        with path.open("a") as stream:
            stream.write(_live_event("a", "RUNNING"))
        wait_for(
            lambda: reader.current().mood is AvatarMood.WORKING,
            timeout=5.0,
            poll_interval=0.02,
            error_message="the watch never woke the reader for the write",
        )
        assert drain_messages(window) == [
            {"type": "avatar_status", "mood": "idle", "is_stale": False},
            {"type": "avatar_status", "mood": "working", "is_stale": False},
        ]
        with path.open("a") as stream:
            stream.write(_live_event("a", "STOPPED"))
        wait_for(
            lambda: reader.current().mood is AvatarMood.IDLE,
            timeout=5.0,
            poll_interval=0.02,
            error_message="the watch never woke the reader for the second write",
        )
    finally:
        reader.stop()


def test_the_reader_starts_watching_once_the_events_directory_appears(tmp_path: Path) -> None:
    path = tmp_path / "mngr" / "agents" / "events.jsonl"
    reader = AvatarStatusReader(
        events_path=path,
        background_tasks_root=tmp_path / "background-tasks",
        broadcaster=WebSocketBroadcaster(),
        debounce_seconds=0.02,
        stale_check_interval_seconds=0.05,
    )
    reader.start()
    try:
        assert reader.current() == AvatarStatus(mood=AvatarMood.IDLE, is_stale=True)
        # The shell creates nothing under an mngr directory: the re-check finds the directory once mngr makes it.
        path.parent.mkdir(parents=True)
        path.write_text(_live_event("a", "RUNNING"))
        wait_for(
            lambda: reader.current() == AvatarStatus(mood=AvatarMood.WORKING, is_stale=False),
            timeout=5.0,
            poll_interval=0.02,
            error_message="the reader never read the file that appeared after its start",
        )
    finally:
        reader.stop()


def test_the_reader_broadcasts_only_when_the_status_changes(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    broadcaster = WebSocketBroadcaster()
    window: queue.Queue[str | None] = broadcaster.register()
    reader = AvatarStatusReader(
        events_path=path, background_tasks_root=tmp_path / "background-tasks", broadcaster=broadcaster
    )
    reader.refresh()
    assert drain_messages(window) == []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps({"timestamp": now, "type": "AGENT_STATE", "agent": _agent("a", "RUNNING")}) + "\n")
    reader.refresh()
    reader.refresh()
    assert drain_messages(window) == [{"type": "avatar_status", "mood": "working", "is_stale": False}]
    assert reader.current() == AvatarStatus(mood=AvatarMood.WORKING, is_stale=False)


def test_the_reader_refolds_a_marker_write_and_removal_through_the_tree_watch(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(_live_event("lead", "WAITING"))
    root = tmp_path / "background-tasks"
    # The chat's own directory under the root is made by its first marker, after the watch started.
    root.mkdir()
    # The periodic re-check is far off: only the watch can wake the loop for the writes below.
    reader = AvatarStatusReader(
        events_path=path,
        background_tasks_root=root,
        broadcaster=WebSocketBroadcaster(),
        debounce_seconds=0.02,
        stale_check_interval_seconds=60.0,
    )
    reader.start()
    try:
        assert reader.current().mood is AvatarMood.IDLE
        write_background_task_marker(root, "agent-lead", "poll", os.getpid())
        wait_for(
            lambda: reader.current().mood is AvatarMood.WORKING,
            timeout=5.0,
            poll_interval=0.02,
            error_message="the watch never woke the reader for the marker",
        )
        remove_background_task_marker(root, "agent-lead", "poll")
        wait_for(
            lambda: reader.current().mood is AvatarMood.IDLE,
            timeout=5.0,
            poll_interval=0.02,
            error_message="the watch never woke the reader for the marker's removal",
        )
    finally:
        reader.stop()


def test_the_reader_reads_a_marker_root_that_appears_after_its_start(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(_live_event("lead", "WAITING"))
    root = tmp_path / "background-tasks"
    reader = AvatarStatusReader(
        events_path=path,
        background_tasks_root=root,
        broadcaster=WebSocketBroadcaster(),
        debounce_seconds=0.02,
        stale_check_interval_seconds=0.05,
    )
    reader.start()
    try:
        write_background_task_marker(root, "agent-lead", "poll", os.getpid())
        wait_for(
            lambda: reader.current().mood is AvatarMood.WORKING,
            timeout=5.0,
            poll_interval=0.02,
            error_message="the reader never read the marker root that appeared after its start",
        )
    finally:
        reader.stop()
