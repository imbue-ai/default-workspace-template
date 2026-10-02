"""Tests for the notes: parsing Claude's note format, correcting a note and its index line, listing, and deleting a
note so that its file and its index line both go while a note a chat changed meanwhile is kept."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from memories.errors import NoteChangedError
from memories.errors import NoteNameError
from memories.errors import NoteNotFoundError
from memories.errors import NoteWriteError
from memories.notes import INDEX_FILENAME
from memories.notes import NoteType
from memories.notes import delete_note
from memories.notes import file_version
from memories.notes import list_notes
from memories.notes import parse_note
from memories.notes import read_index
from memories.notes import read_note
from memories.notes import render_note
from memories.notes import rewrite_index_hook
from memories.notes import split_index_lines
from memories.notes import update_note
from memories.notes import write_atomically

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


def test_parse_note_reads_claudes_frontmatter_with_the_type_nested_under_metadata() -> None:
    parsed = parse_note(_NOTE)

    assert parsed.name == "units-preference"
    assert parsed.description == "Prefers metric units"
    assert parsed.note_type == NoteType.FEEDBACK
    assert parsed.body == "Use kilometres and kilograms.\n"


def test_parse_note_accepts_a_top_level_type_quoted_values_and_unknown_keys() -> None:
    parsed = parse_note(
        "---\nname: \"role\"\ndescription: 'Is a designer'\ntype: User\nmodified: 2026-10-01T10:00:00Z\n"
        "metadata:\n  originSessionId: abc\n---\nBody\n"
    )

    assert parsed.name == "role"
    assert parsed.description == "Is a designer"
    assert parsed.note_type == NoteType.USER
    assert "  originSessionId: abc" in parsed.frontmatter_lines


@pytest.mark.parametrize(
    "text",
    ["Just a body, no frontmatter.\n", "---\nname: never closed\nbody\n", ""],
)
def test_a_note_without_complete_frontmatter_is_all_body(text: str) -> None:
    parsed = parse_note(text)

    assert parsed.name is None
    assert parsed.description is None
    assert parsed.note_type == NoteType.OTHER
    assert parsed.body == text


def test_an_unrecognised_type_reads_as_other() -> None:
    assert parse_note("---\nmetadata:\n  type: hobby\n---\nx\n").note_type == NoteType.OTHER


def test_render_note_replaces_the_summary_and_body_and_keeps_every_other_frontmatter_line() -> None:
    rendered = render_note(parse_note(_NOTE), "Prefers  metric\nunits always", "  Kilometres only.  ")

    assert rendered == (
        '---\nname: units-preference\ndescription: "Prefers metric units always"\nmetadata:\n  type: feedback\n---\n\n'
        "Kilometres only.\n"
    )


def test_render_note_adds_a_summary_after_the_name_when_there_was_none() -> None:
    rendered = render_note(parse_note("---\nname: role\nmetadata:\n  type: user\n---\nx\n"), "Is a designer", "x")

    assert rendered.startswith('---\nname: role\ndescription: "Is a designer"\nmetadata:\n')


def test_rewrite_index_hook_rewrites_only_that_notes_line() -> None:
    index = "# Memory\n- [Units](units.md) — old summary\n- [Role](role.md) — Is a designer\n"

    assert rewrite_index_hook(index, "units.md", "New\nsummary") == (
        "# Memory\n- [Units](units.md) — New summary\n- [Role](role.md) — Is a designer\n"
    )


def test_update_note_rewrites_the_note_and_its_index_line_and_returns_the_new_version(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path)

    note = update_note(notes_dir, "units.md", "Prefers metric", "Kilometres.", file_version(notes_dir / "units.md"))

    assert note.description == "Prefers metric"
    assert note.body == "Kilometres.\n"
    assert note.version == file_version(notes_dir / "units.md")
    assert "- [Units](units.md) — Prefers metric\n" in read_index(notes_dir)
    assert "metadata:\n  type: feedback" in (notes_dir / "units.md").read_text()


def test_update_note_refuses_a_stale_version_and_leaves_the_note_and_index_alone(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path)
    index_before = read_index(notes_dir)

    with pytest.raises(NoteChangedError):
        update_note(notes_dir, "units.md", "Changed", "Changed", "0-0")

    assert (notes_dir / "units.md").read_text() == _NOTE
    assert read_index(notes_dir) == index_before


def test_list_notes_lists_note_files_newest_first_and_skips_the_index_readme_and_other_files(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path)
    (notes_dir / "README.md").write_text("# data/memories/\n")
    (notes_dir / ".units.md.123.tmp").write_text("half written")
    (notes_dir / "notes.txt").write_text("not markdown")
    (notes_dir / "subfolder.md").mkdir()
    os.utime(notes_dir / "units.md", (1_000_000, 1_000_000))
    os.utime(notes_dir / "role.md", (2_000_000, 2_000_000))

    listing = list_notes(notes_dir)

    assert [note.file_name for note in listing.notes] == ["role.md", "units.md"]
    assert listing.notes[1].raw_text == _NOTE
    assert listing.unreadable_file_names == ()
    assert list_notes(tmp_path / "no-such-folder").notes == ()


def test_a_note_with_no_summary_is_described_by_its_file_name(tmp_path: Path) -> None:
    notes_dir = tmp_path / "memories"
    notes_dir.mkdir()
    (notes_dir / "likes_dark-mode.md").write_text("Prefers dark mode.\n")

    (note,) = list_notes(notes_dir).notes

    assert note.description == "likes dark mode"
    assert note.note_type == NoteType.OTHER


def test_read_index_is_empty_when_there_is_no_index(tmp_path: Path) -> None:
    assert read_index(tmp_path) == ""


def test_concurrent_writes_of_one_file_each_land_whole(tmp_path: Path) -> None:
    target = tmp_path / "MEMORY.md"
    texts = [f"- [Note {idx}](note-{idx}.md) — {'x' * 2000}\n" for idx in range(16)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda text: write_atomically(target, text), texts))

    assert target.read_text() in texts
    assert [path.name for path in tmp_path.iterdir()] == ["MEMORY.md"]


def test_write_atomically_replaces_the_file_and_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    target.write_text("old")

    write_atomically(target, "new")

    assert target.read_text() == "new"
    assert [path.name for path in tmp_path.iterdir()] == ["note.md"]
    with pytest.raises(NoteWriteError):
        write_atomically(tmp_path / "missing-folder" / "note.md", "x")
    assert [path.name for path in tmp_path.iterdir()] == ["note.md"]


def test_a_note_says_which_harness_saved_it() -> None:
    pi_note = parse_note("---\nname: units\nmetadata:\n  type: feedback\n  source: pi-coding\n---\nx\n")
    claude_note = parse_note("---\nname: role\nmetadata:\n  type: user\n  originSessionId: abc-123\n---\nx\n")
    top_level = parse_note("---\nname: role\nsource: codex\n---\nx\n")
    unknown = parse_note("---\nname: role\nmetadata:\n  type: user\n---\nx\n")

    assert pi_note.source == "pi-coding"
    assert claude_note.source == "claude"
    assert top_level.source == "codex"
    assert unknown.source is None
    assert parse_note("no frontmatter").source is None


@pytest.mark.parametrize(
    "summary", ["Uses: tabs", "- starts with a dash", "[bracketed]", 'says "hello"', "it's fine", "  spaced  "]
)
def test_a_summary_round_trips_through_the_frontmatter_whatever_it_contains(summary: str) -> None:
    rendered = render_note(parse_note(_NOTE), summary, "Body.")

    assert parse_note(rendered).description == " ".join(summary.split())


def test_single_quoted_and_double_quoted_values_are_read_as_yaml_writes_them() -> None:
    parsed = parse_note("---\nname: 'it''s'\ndescription: \"say \\\"hi\\\"\"\n---\nx\n")

    assert parsed.name == "it's"
    assert parsed.description == 'say "hi"'


def test_a_note_s_version_is_read_before_its_text(tmp_path: Path) -> None:
    """A chat writing between the two reads must make the version older than the text, so a save is refused."""
    notes_dir = _notes_dir(tmp_path)
    version_before = file_version(notes_dir / "units.md")

    note = read_note(notes_dir / "units.md")

    assert note.version == version_before
