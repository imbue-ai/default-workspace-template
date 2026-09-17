"""Unit tests for how an event is stored: the per-field cap `write_event` applies."""

from __future__ import annotations

import json
from pathlib import Path

from host_backup.events import BackupEventType, make_event, write_event


def test_a_huge_restic_stdout_is_capped_at_both_ends_when_it_is_written(
    tmp_path: Path,
) -> None:
    """Nothing rotates the events log, so an uncapped progress stream grew it by
    megabytes a day. What an operator reads -- the opening lines and the final
    summary -- has to survive the cap, so both ends are kept."""
    events_dir = tmp_path / "events"
    stdout = "HEAD-MARKER\n" + ("progress tick\n" * 200_000) + "TAIL-SUMMARY"
    write_event(
        events_dir,
        make_event(
            BackupEventType.RESTIC_BACKUP_SUCCEEDED, tick_id="t1", stdout=stdout
        ),
    )

    written = (events_dir / "events.jsonl").read_text()
    stored = json.loads(written)["stdout"]
    assert len(written) < len(stdout) / 100
    assert stored.startswith("HEAD-MARKER")
    assert stored.endswith("TAIL-SUMMARY")
    assert "characters dropped" in stored


def test_an_ordinary_event_is_stored_verbatim(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    write_event(
        events_dir,
        make_event(
            BackupEventType.RESTIC_BACKUP_FAILED, tick_id="t1", stdout="repo locked"
        ),
    )

    assert json.loads((events_dir / "events.jsonl").read_text())["stdout"] == "repo locked"
