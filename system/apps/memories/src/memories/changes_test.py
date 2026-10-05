"""Tests for the record of the user's deletes and edits: what a line holds, and how old lines are dropped."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from memories.changes import CHANGE_MAX_AGE
from memories.changes import NoteChange
from memories.changes import NoteChangeKind
from memories.changes import parse_changes
from memories.changes import read_changes
from memories.changes import record_note_change
from memories.errors import NoteWriteError

_NOW = datetime(2026, 10, 1, 22, 4, 32, tzinfo=timezone.utc)


def test_a_change_is_one_json_line_with_the_file_name_the_change_and_the_time(tmp_path: Path) -> None:
    log_path = tmp_path / "state" / "user-changes.jsonl"

    record_note_change(log_path, "user-profile.md", NoteChangeKind.DELETED, _NOW)

    assert log_path.read_text() == '{"file_name":"user-profile.md","change":"DELETED","at":"2026-10-01T22:04:32Z"}\n'


def test_changes_accumulate_and_ones_past_the_age_limit_are_dropped(tmp_path: Path) -> None:
    log_path = tmp_path / "user-changes.jsonl"
    record_note_change(log_path, "old.md", NoteChangeKind.EDITED, _NOW - CHANGE_MAX_AGE - timedelta(seconds=1))
    record_note_change(log_path, "kept.md", NoteChangeKind.EDITED, _NOW - CHANGE_MAX_AGE)

    record_note_change(log_path, "new.md", NoteChangeKind.DELETED, _NOW)

    assert [change.file_name for change in read_changes(log_path)] == ["kept.md", "new.md"]


def test_lines_that_are_not_changes_are_skipped() -> None:
    text = (
        '{"file_name":"a.md","change":"DELETED","at":"2026-10-01T22:04:32Z"}\n'
        "not json\n"
        '{"file_name":"b.md","change":"RENAMED","at":"2026-10-01T22:04:32Z"}\n'
        '{"file_name":"c.md"}\n'
    )

    assert parse_changes(text) == [NoteChange(file_name="a.md", change=NoteChangeKind.DELETED, at=_NOW)]


def test_a_missing_record_reads_as_no_changes(tmp_path: Path) -> None:
    assert read_changes(tmp_path / "missing.jsonl") == []


def test_a_record_that_cannot_be_written_raises_a_write_error(tmp_path: Path) -> None:
    (tmp_path / "state").write_text("a file, not a folder")

    with pytest.raises(NoteWriteError):
        record_note_change(tmp_path / "state" / "user-changes.jsonl", "a.md", NoteChangeKind.DELETED, _NOW)
