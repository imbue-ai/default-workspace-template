"""Tests for deleting a note: its file and its index line both go, and a note a chat changed meanwhile is kept."""

from pathlib import Path

import pytest

from memories.errors import NoteChangedError
from memories.errors import NoteNameError
from memories.errors import NoteNotFoundError
from memories.notes import INDEX_FILENAME
from memories.notes import delete_note
from memories.notes import file_version
from memories.notes import split_index_lines

_NOTE = """---
name: units-preference
description: Prefers metric units
metadata:
  type: feedback
---

Use kilometres and kilograms.
"""


def _notes_dir(tmp_path: Path) -> Path:
    notes_dir = tmp_path / "memories"
    notes_dir.mkdir()
    (notes_dir / "units.md").write_text(_NOTE)
    (notes_dir / "role.md").write_text("---\nname: role\ndescription: Is a designer\n---\n\nWorks on the app.\n")
    (notes_dir / INDEX_FILENAME).write_text(
        "- [Units](units.md) — Prefers metric units\n- [Role](role.md) — Is a designer\n"
    )
    return notes_dir


def test_delete_erases_the_note_and_its_index_line_and_keeps_the_others(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path)

    delete_note(notes_dir, "units.md", file_version(notes_dir / "units.md"))

    assert not (notes_dir / "units.md").exists()
    assert (notes_dir / "role.md").is_file()
    assert (notes_dir / INDEX_FILENAME).read_text() == "- [Role](role.md) — Is a designer\n"
    assert sorted(path.name for path in tmp_path.rglob("*")) == ["MEMORY.md", "memories", "role.md"]


def test_delete_works_when_the_note_has_no_index_line_or_there_is_no_index(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path)
    (notes_dir / INDEX_FILENAME).unlink()

    delete_note(notes_dir, "units.md", file_version(notes_dir / "units.md"))

    assert not (notes_dir / "units.md").exists()
    assert not (notes_dir / INDEX_FILENAME).exists()


def test_delete_refuses_a_note_a_chat_changed_since_it_was_read(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path)
    stale_version = file_version(notes_dir / "units.md")
    (notes_dir / "units.md").write_text(_NOTE + "\nAlso Celsius.\n")

    with pytest.raises(NoteChangedError):
        delete_note(notes_dir, "units.md", stale_version)

    assert (notes_dir / "units.md").read_text().endswith("Also Celsius.\n")
    assert "units.md" in (notes_dir / INDEX_FILENAME).read_text()


def test_delete_refuses_names_outside_the_notes_and_the_index_itself(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path)

    with pytest.raises(NoteNameError):
        delete_note(notes_dir, "../outside.md", "0-0")
    with pytest.raises(NoteNameError):
        delete_note(notes_dir, INDEX_FILENAME, file_version(notes_dir / INDEX_FILENAME))
    with pytest.raises(NoteNotFoundError):
        delete_note(notes_dir, "missing.md", "0-0")
    assert (notes_dir / INDEX_FILENAME).is_file()


def test_split_index_lines_matches_the_file_exactly_not_a_prefix() -> None:
    index = "- [A](units.md) — a\n- [B](units.md.bak.md) — b\n* [C](units.md) — again\n"

    kept, removed = split_index_lines(index, "units.md")

    assert kept == "- [B](units.md.bak.md) — b\n"
    assert removed == ("- [A](units.md) — a", "* [C](units.md) — again")
