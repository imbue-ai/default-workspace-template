from pathlib import Path

import pytest

from agent_memory.notes import (
    INDEX_LINE_LIMIT,
    InvalidNoteError,
    NoteChangedError,
    NoteDraft,
    NoteKind,
    NoteNotFoundError,
    NoteStore,
    index_titles,
    pointer_line,
    readable_text,
)

NOTE = """---
name: likes-tea
description: User likes tea
metadata:
  node_type: memory
  type: user
  originSessionId: 842d7ff2
---

The user likes tea.

**Why:** Said so.
"""

INDEX = """# User Preferences

- [Likes tea](likes-tea.md) — user likes tea
- Always add emoticons
"""


def test_a_note_reads_as_its_summary_and_text_without_the_bookkeeping() -> None:
    text = readable_text("likes-tea.md", NOTE)
    assert text == "Summary: User likes tea\n\nThe user likes tea.\n\nWhy: Said so."
    assert "originSessionId" not in (text or "") and "name:" not in (text or "")


def test_the_index_reads_as_titles_and_summaries() -> None:
    assert (
        readable_text("MEMORY.md", INDEX)
        == "User Preferences\n\nLikes tea — user likes tea\nAlways add emoticons"
    )


def test_a_missing_file_stays_missing() -> None:
    assert readable_text("likes-tea.md", None) is None


def test_the_index_names_each_note_s_title() -> None:
    assert index_titles(INDEX) == {"likes-tea.md": "Likes tea"}


# The note store, as the Memory app drives it.


def _store(tmp_path: Path) -> NoteStore:
    return NoteStore(folder=tmp_path / "memories")


def _draft(
    title: str, summary: str = "a summary", body: str = "remember this"
) -> NoteDraft:
    return NoteDraft(title=title, summary=summary, kind=NoteKind.USER, body=body)


def test_a_new_note_gets_a_file_and_an_index_line(tmp_path: Path) -> None:
    store = _store(tmp_path)
    note = store.create(_draft("Likes tea", "user likes tea"))
    assert note.file_name == "likes-tea.md"
    assert (
        store.index_path.read_text() == "- [Likes tea](likes-tea.md) — user likes tea\n"
    )
    assert [listed.id for listed in store.list_notes()] == ["likes-tea"]


def test_a_title_with_square_brackets_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(InvalidNoteError, match="square brackets"):
        store.create(_draft("Likes [green] tea"))
    note = store.create(_draft("Likes tea"))
    with pytest.raises(InvalidNoteError, match="square brackets"):
        store.update(note.id, note.version, _draft("Likes tea]"))
    assert not (store.folder / "likes-green-tea.md").exists()


def test_a_new_title_renames_the_file_and_every_link_to_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tea = store.create(_draft("Likes tea"))
    store.create(
        _draft(
            "Morning routine",
            body="Starts with tea, see [[likes-tea]] and [[ likes-tea ]].",
        )
    )
    store.index_path.write_text(
        store.index_path.read_text() + "- Ask before [[likes-tea]] changes\n"
    )
    renamed = store.update(tea.id, tea.version, _draft("Likes green tea"))
    assert renamed.id == "likes-green-tea"
    assert renamed.name == "likes-green-tea"
    assert not (store.folder / "likes-tea.md").exists()
    assert "name: likes-green-tea" in (store.folder / "likes-green-tea.md").read_text()
    routine = (store.folder / "morning-routine.md").read_text()
    assert "[[likes-green-tea]] and [[likes-green-tea]]" in routine
    assert store.index_path.read_text().splitlines() == [
        "- [Likes green tea](likes-green-tea.md) — a summary",
        "- [Morning routine](morning-routine.md) — a summary",
        "- Ask before [[likes-green-tea]] changes",
    ]


def test_a_new_title_that_slugs_the_same_keeps_the_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tea = store.create(_draft("Likes tea"))
    kept = store.update(tea.id, tea.version, _draft("Likes Tea!"))
    assert kept.id == "likes-tea"
    assert store.index_path.read_text() == "- [Likes Tea!](likes-tea.md) — a summary\n"


def test_a_rename_onto_a_taken_name_takes_the_next_free_one(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(_draft("Coffee"))
    tea = store.create(_draft("Tea"))
    renamed = store.update(tea.id, tea.version, _draft("Coffee"))
    assert renamed.file_name == "coffee-2.md"
    assert sorted(path.name for path in store.folder.glob("*.md")) == [
        "MEMORY.md",
        "coffee-2.md",
        "coffee.md",
    ]


def test_an_index_line_past_the_limit_is_flagged_and_still_saved(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.create(_draft("Short"))
    store.create(_draft("Long", summary="x" * INDEX_LINE_LIMIT))
    entries = store.outline(store.list_notes()).entries
    assert [(entry.text, entry.is_too_long) for entry in entries] == [
        ("Short", False),
        ("Long", True),
    ]
    assert entries[1].length == len(
        pointer_line("Long", "long.md", "x" * INDEX_LINE_LIMIT)
    )


def test_saving_a_note_written_into_the_index_gives_it_a_file_in_its_place(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.folder.mkdir()
    store.index_path.write_text("# Prefs\n\n- Always add emoticons\n")
    (loose,) = store.list_notes()
    assert loose.is_index_only
    saved = store.update(loose.id, loose.version, _draft("Emoticons", "add emoticons"))
    assert saved.file_name == "emoticons.md"
    assert (
        store.index_path.read_text()
        == "# Prefs\n\n- [Emoticons](emoticons.md) — add emoticons\n"
    )


def test_a_note_changed_elsewhere_is_not_overwritten(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tea = store.create(_draft("Tea"))
    (store.folder / "tea.md").write_text(tea.raw + "edited by hand\n")
    with pytest.raises(NoteChangedError):
        store.update(tea.id, tea.version, _draft("Tea", body="new"))
    with pytest.raises(NoteChangedError):
        store.delete(tea.id, tea.version)


def test_deleting_a_note_removes_its_file_and_its_index_line(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tea = store.create(_draft("Tea"))
    store.create(_draft("Coffee"))
    store.delete(tea.id, tea.version)
    assert not (store.folder / "tea.md").exists()
    assert store.index_path.read_text() == "- [Coffee](coffee.md) — a summary\n"
    with pytest.raises(NoteNotFoundError):
        store.delete(tea.id, tea.version)
