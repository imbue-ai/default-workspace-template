import json
from pathlib import Path

import pytest

from memory.changes import ChangeLog
from memory.changes import ChangedSinceError


def _record(changes_dir: Path, change_id: str, files: list[dict]) -> None:
    changes_dir.mkdir(parents=True, exist_ok=True)
    (changes_dir / f"{change_id}.json").write_text(
        json.dumps({"id": change_id, "time": "2026-11-18T00:00:00+00:00", "summary": "s", "files": files, "undone_at": None})
    )


def test_undo_restores_an_edited_note_and_removes_an_added_one(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "MEMORY.md").write_text("new index")
    (notes / "tea.md").write_text("tea")
    _record(
        tmp_path / "changes",
        "c1",
        [{"name": "MEMORY.md", "before": "old index", "after": "new index"}, {"name": "tea.md", "before": None, "after": "tea"}],
    )
    log = ChangeLog(tmp_path / "changes", notes)
    undone = log.undo("c1", force=False)
    assert (notes / "MEMORY.md").read_text() == "old index"
    assert not (notes / "tea.md").exists()
    assert undone.undone_at is not None
    assert log.list_changes()[0].undone_at == undone.undone_at


def test_undo_refuses_when_the_note_changed_again_unless_forced(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "tea.md").write_text("edited later")
    _record(tmp_path / "changes", "c1", [{"name": "tea.md", "before": "v1", "after": "v2"}])
    log = ChangeLog(tmp_path / "changes", notes)
    with pytest.raises(ChangedSinceError):
        log.undo("c1", force=False)
    assert (notes / "tea.md").read_text() == "edited later"
    log.undo("c1", force=True)
    assert (notes / "tea.md").read_text() == "v1"


def test_undo_brings_back_a_deleted_note(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    _record(tmp_path / "changes", "c1", [{"name": "tea.md", "before": "v1", "after": None}])
    ChangeLog(tmp_path / "changes", notes).undo("c1", force=False)
    assert (notes / "tea.md").read_text() == "v1"
