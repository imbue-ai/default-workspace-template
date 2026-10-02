"""Tests for the notes: parsing Claude's note format, correcting a note and its index line, listing, and deleting a
note so that its file and its index line both go while a note a chat changed meanwhile is kept."""

import fcntl
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from memories.errors import NoteChangedError
from memories.errors import NoteNameError
from memories.errors import NoteNotFoundError
from memories.errors import NoteWriteError
from memories.notes import INDEX_FILENAME
from memories.notes import INDEX_LOADED_MAX_BYTES
from memories.notes import INDEX_LOADED_MAX_LINES
from memories.notes import INDEX_LOCK_FILENAME
from memories.notes import IndexEntry
from memories.notes import NoteType
from memories.notes import delete_note
from memories.notes import file_version
from memories.notes import index_lock
from memories.notes import list_notes
from memories.notes import loaded_line_count
from memories.notes import parse_index
from memories.notes import parse_note
from memories.notes import read_index
from memories.notes import relist_in_index
from memories.notes import render_note
from memories.notes import rewrite_index_hook
from memories.notes import split_index_lines
from memories.notes import summarize_index
from memories.notes import unlist_from_index
from memories.notes import update_note
from memories.notes import validate_note_name
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
    unlist_from_index(notes_dir, "units.md")

    assert not (notes_dir / "units.md").exists()
    assert (notes_dir / "role.md").is_file()
    assert (notes_dir / INDEX_FILENAME).read_text() == "- [Role](role.md) — Is a designer\n"
    assert sorted(path.name for path in tmp_path.rglob("*")) == [
        INDEX_LOCK_FILENAME,
        "MEMORY.md",
        "memories",
        "role.md",
    ]


def test_delete_works_when_the_note_has_no_index_line_or_there_is_no_index(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path)
    (notes_dir / INDEX_FILENAME).unlink()

    delete_note(notes_dir, "units.md", file_version(notes_dir / "units.md"))
    unlist_from_index(notes_dir, "units.md")

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

    assert rewrite_index_hook(index, "units.md", "Units", "New\nsummary") == (
        "# Memory\n- [Units](units.md) — New summary\n- [Role](role.md) — Is a designer\n"
    )


def test_update_note_rewrites_the_note_and_its_index_line_and_returns_the_new_version(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path)

    note = update_note(notes_dir, "units.md", "Prefers metric", "Kilometres.", file_version(notes_dir / "units.md"))
    assert "- [Units](units.md) — Prefers metric\n" not in read_index(notes_dir)
    relist_in_index(notes_dir, note)

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


def test_parse_index_finds_each_notes_first_line() -> None:
    index = "# Memory\n- [Units](units.md) — Prefers metric\n* [Role](role.md) - Is a designer\n- [Again](units.md) — dup\n"

    assert parse_index(index) == {
        "units.md": IndexEntry(is_loaded=True),
        "role.md": IndexEntry(is_loaded=True),
    }


def test_lines_past_200_or_25kb_are_not_loaded() -> None:
    many = "".join(f"- [N{idx}](n{idx}.md) — hook\n" for idx in range(INDEX_LOADED_MAX_LINES + 2))
    entries = parse_index(many)

    assert entries[f"n{INDEX_LOADED_MAX_LINES - 1}.md"].is_loaded is True
    assert entries[f"n{INDEX_LOADED_MAX_LINES}.md"].is_loaded is False
    assert loaded_line_count(many) == INDEX_LOADED_MAX_LINES

    heavy = "\n".join(f"- [Long](l{idx}.md) — " + "x" * 5000 for idx in range(10))
    assert loaded_line_count(heavy) == 5
    assert parse_index(heavy)["l4.md"].is_loaded is True
    assert parse_index(heavy)["l5.md"].is_loaded is False


def test_a_line_ending_exactly_at_25kb_is_loaded_and_one_byte_more_is_not() -> None:
    first = "- [A](a.md) — " + "x" * 100
    filler_bytes = (
        INDEX_LOADED_MAX_BYTES - (len(first.encode("utf-8")) + 1) - len("- [B](b.md) — ".encode("utf-8")) - 1
    )
    exactly = f"{first}\n- [B](b.md) — {'y' * filler_bytes}\n"
    over = f"{first}\n- [B](b.md) — {'y' * (filler_bytes + 1)}\n"

    assert len(exactly.encode("utf-8")) == INDEX_LOADED_MAX_BYTES
    assert parse_index(exactly)["b.md"].is_loaded is True
    assert parse_index(over)["b.md"].is_loaded is False


def test_editing_a_note_missing_from_the_index_adds_its_line_back(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path)
    (notes_dir / INDEX_FILENAME).write_text("- [Role](role.md) — Is a designer")

    relist_in_index(
        notes_dir, update_note(notes_dir, "units.md", "Prefers metric", "Km.", file_version(notes_dir / "units.md"))
    )

    assert read_index(notes_dir) == (
        "- [Role](role.md) — Is a designer\n- [Units preference](units.md) — Prefers metric\n"
    )


def test_editing_a_note_creates_the_index_when_there_is_none(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path)
    (notes_dir / INDEX_FILENAME).unlink()

    relist_in_index(
        notes_dir, update_note(notes_dir, "units.md", "Prefers metric", "Km.", file_version(notes_dir / "units.md"))
    )

    assert read_index(notes_dir) == "- [Units preference](units.md) — Prefers metric\n"


def test_summarize_index_counts_lines_and_names_listed_notes_that_are_gone() -> None:
    index = "- [Units](units.md) — x\n- [Gone](gone.md) — y\n"

    summary = summarize_index(index, {"units.md", "unlisted.md"})

    assert (summary.line_count, summary.loaded_line_count) == (2, 2)
    assert (summary.max_lines, summary.max_bytes) == (INDEX_LOADED_MAX_LINES, INDEX_LOADED_MAX_BYTES)
    assert summary.missing_files == ("gone.md",)


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


def test_any_visible_markdown_file_is_a_note_and_is_not_called_missing(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path)
    for name in ("my note.md", "_draft.md", "café.md"):
        (notes_dir / name).write_text("---\ndescription: odd name\n---\nx\n")
    (notes_dir / ".hidden.md").write_text("---\ndescription: hidden\n---\nx\n")
    index = read_index(notes_dir) + "- [Odd](my note.md) — odd name\n"

    listed = {note.file_name for note in list_notes(notes_dir).notes}

    assert listed == {"units.md", "role.md", "my note.md", "_draft.md", "café.md"}
    assert summarize_index(index, listed).missing_files == ()


@pytest.mark.parametrize(
    "file_name", ["../outside.md", "a/b.md", ".hidden.md", "MEMORY.md", "README.md", ".md", "x.txt"]
)
def test_names_that_are_not_a_visible_markdown_file_in_the_folder_are_refused(file_name: str) -> None:
    with pytest.raises(NoteNameError):
        validate_note_name(file_name)


def test_a_block_scalar_summary_reads_as_none_and_an_edit_replaces_all_its_lines() -> None:
    text = "---\nname: units\ndescription: >\n  spans\n  lines\nmetadata:\n  type: feedback\n---\nbody\n"

    parsed = parse_note(text)
    rendered = render_note(parsed, "Prefers metric", "body")

    assert parsed.description is None
    assert parsed.note_type == NoteType.FEEDBACK
    assert rendered == ('---\nname: units\ndescription: "Prefers metric"\nmetadata:\n  type: feedback\n---\n\nbody\n')


def test_a_repeated_key_reads_as_its_first_value_the_one_an_edit_rewrites() -> None:
    parsed = parse_note("---\ndescription: first\ndescription: second\n---\nx\n")

    assert parsed.description == "first"
    assert parse_note(render_note(parsed, "edited", "x")).description == "edited"


def test_rewriting_a_file_keeps_its_permissions(tmp_path: Path) -> None:
    path = tmp_path / "MEMORY.md"
    path.write_text("old")
    path.chmod(0o600)

    write_atomically(path, "new")

    assert (path.read_text(), path.stat().st_mode & 0o777) == ("new", 0o600)


def test_an_index_with_a_bad_byte_still_reads_and_rewrites(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path)
    (notes_dir / INDEX_FILENAME).write_bytes(b"# Memory \xff\n- [Units](units.md) \xe2\x80\x94 Prefers metric units\n")

    unlist_from_index(notes_dir, "units.md")

    assert read_index(notes_dir) == "# Memory �\n"


def test_the_index_lock_is_exclusive_on_the_file_the_chats_lock_too(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path)

    with index_lock(notes_dir):
        with (notes_dir / ".MEMORY.md.lock").open("a") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
