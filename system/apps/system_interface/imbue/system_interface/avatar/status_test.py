"""Tests for the mood fold over mngr's agents event file and the reader that watches it."""

import json
import queue
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

from imbue.system_interface.avatar.designs import AvatarMood
from imbue.system_interface.avatar.status import AvatarStatus
from imbue.system_interface.avatar.status import AvatarStatusReader
from imbue.system_interface.avatar.status import STALE_AFTER
from imbue.system_interface.avatar.status import agent_events_path
from imbue.system_interface.avatar.status import fold_agent_events
from imbue.system_interface.avatar.status import parse_event_timestamp
from imbue.system_interface.avatar.status import read_avatar_status
from imbue.system_interface.avatar.status import read_tail_lines_back_to_snapshot
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


def test_the_tail_read_stops_at_the_last_snapshot_and_skips_a_cut_line(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    filler = _event("AGENT_STATE", agent=_agent("filler", "STOPPED", is_primary=False)) + " " * 200
    lines = [filler] * 2000 + [_event("AGENTS_FULL_STATE", agents=[_agent("a", "RUNNING")])] + [filler] * 3
    path.write_text("\n".join(lines) + "\n")
    tail = read_tail_lines_back_to_snapshot(path)
    assert len(tail) < len(lines)
    assert all(json.loads(line) for line in tail if line.strip())
    assert '"AGENTS_FULL_STATE"' in tail[0] or any('"AGENTS_FULL_STATE"' in line for line in tail)
    assert read_avatar_status(path, _NOW) == AvatarStatus(mood=AvatarMood.WORKING, is_stale=False)


def test_an_absent_file_reads_stale_and_idle(tmp_path: Path) -> None:
    assert read_avatar_status(tmp_path / "missing.jsonl", _NOW) == AvatarStatus(mood=AvatarMood.IDLE, is_stale=True)


def test_the_events_path_follows_the_host_directory_variable(tmp_path: Path) -> None:
    assert agent_events_path({"MNGR_HOST_DIR": str(tmp_path)}) == tmp_path / "events/mngr/agents/events.jsonl"
    assert agent_events_path({}) == Path.home() / ".mngr/events/mngr/agents/events.jsonl"


def test_the_reader_broadcasts_only_when_the_status_changes(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    broadcaster = WebSocketBroadcaster()
    window: queue.Queue[str | None] = broadcaster.register()
    reader = AvatarStatusReader(events_path=path, broadcaster=broadcaster)
    reader.refresh()
    assert drain_messages(window) == []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps({"timestamp": now, "type": "AGENT_STATE", "agent": _agent("a", "RUNNING")}) + "\n")
    reader.refresh()
    reader.refresh()
    assert drain_messages(window) == [{"type": "avatar_status", "mood": "working", "is_stale": False}]
    assert reader.current() == AvatarStatus(mood=AvatarMood.WORKING, is_stale=False)
