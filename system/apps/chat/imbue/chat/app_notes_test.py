from datetime import datetime
from pathlib import Path

from imbue.chat.app_notes import APP_NOTES_DIRNAME
from imbue.chat.app_notes import AppNote
from imbue.chat.app_notes import AppNoteStore
from imbue.chat.app_notes import MAX_NOTES_KEPT
from imbue.chat.app_notes import MAX_NOTES_SERVED
from imbue.chat.app_notes import app_note_frame

_CHAT = "agent-00000000000000000000000000000041"
_OTHER_CHAT = "agent-00000000000000000000000000000042"


def _note_line(index: int) -> str:
    return (
        AppNote(
            note_id=f"n{index}", timestamp=f"2026-11-18T00:00:00.{index:06d}+00:00", source="App", text=f"line {index}"
        ).model_dump_json()
        + "\n"
    )


def test_a_note_is_kept_on_disk_per_chat_and_read_back_oldest_first(tmp_path: Path) -> None:
    root = tmp_path / APP_NOTES_DIRNAME
    store = AppNoteStore(root=root)

    first = store.add(_CHAT, "AI Usage Limits", "Claude usage 2% (Current session)")
    second = store.add(_CHAT, "AI Usage Limits", "Claude usage 3% (Current session)")
    store.add(_OTHER_CHAT, "AI Usage Limits", "Claude usage 9% (Weekly)")

    # A fresh store over the same folder (a restart) reads what the first one wrote.
    reread = AppNoteStore(root=root).notes(_CHAT)
    assert [(note.note_id, note.source, note.text) for note in reread] == [
        (first.note_id, "AI Usage Limits", "Claude usage 2% (Current session)"),
        (second.note_id, "AI Usage Limits", "Claude usage 3% (Current session)"),
    ]
    assert first.note_id != second.note_id
    assert datetime.fromisoformat(first.timestamp).utcoffset() is not None
    assert sorted(path.name for path in root.iterdir()) == [f"{_CHAT}.jsonl", f"{_OTHER_CHAT}.jsonl"]


def test_a_chat_without_notes_reads_none(tmp_path: Path) -> None:
    assert AppNoteStore(root=tmp_path / APP_NOTES_DIRNAME).notes(_CHAT) == ()
    assert AppNoteStore(root=None).notes(_CHAT) == ()


def test_an_unreadable_line_is_skipped_and_the_rest_still_read(tmp_path: Path) -> None:
    root = tmp_path / APP_NOTES_DIRNAME
    root.mkdir()
    (root / f"{_CHAT}.jsonl").write_text(_note_line(1) + "not json\n\n" + '{"note_id": "x"}\n' + _note_line(2))

    assert [note.note_id for note in AppNoteStore(root=root).notes(_CHAT)] == ["n1", "n2"]


def test_the_page_reads_only_the_newest_notes(tmp_path: Path) -> None:
    root = tmp_path / APP_NOTES_DIRNAME
    root.mkdir()
    (root / f"{_CHAT}.jsonl").write_text("".join(_note_line(i) for i in range(MAX_NOTES_SERVED + 5)))

    notes = AppNoteStore(root=root).notes(_CHAT)

    assert len(notes) == MAX_NOTES_SERVED
    assert (notes[0].note_id, notes[-1].note_id) == ("n5", f"n{MAX_NOTES_SERVED + 4}")


def test_a_log_past_its_bound_is_cut_back_to_its_newest_notes(tmp_path: Path) -> None:
    root = tmp_path / APP_NOTES_DIRNAME
    root.mkdir()
    log = root / f"{_CHAT}.jsonl"
    log.write_text("".join(_note_line(i) for i in range(MAX_NOTES_KEPT)))
    store = AppNoteStore(root=root)

    # At the bound the log is left as it is.
    assert len(log.read_text().splitlines()) == MAX_NOTES_KEPT
    newest = store.add(_CHAT, "App", "one past the bound")

    lines = log.read_text().splitlines()
    assert len(lines) == MAX_NOTES_SERVED
    assert AppNote.model_validate_json(lines[0]).note_id == f"n{MAX_NOTES_KEPT - MAX_NOTES_SERVED + 1}"
    assert AppNote.model_validate_json(lines[-1]).note_id == newest.note_id
    assert not log.with_suffix(".tmp").exists()


def test_notes_held_in_memory_are_bounded_the_same_way() -> None:
    store = AppNoteStore(root=None)
    for index in range(MAX_NOTES_KEPT + 1):
        store.add(_CHAT, "App", f"line {index}")

    notes = store.notes(_CHAT)
    assert len(notes) == MAX_NOTES_SERVED
    assert notes[-1].text == f"line {MAX_NOTES_KEPT}"
    assert store.notes(_OTHER_CHAT) == ()


def test_a_notes_frame_carries_the_note_under_its_own_type() -> None:
    note = AppNote(note_id="n1", timestamp="2026-11-18T00:00:00+00:00", source="App", text="hello")

    assert app_note_frame(note) == {
        "type": "app_note",
        "note_id": "n1",
        "timestamp": "2026-11-18T00:00:00+00:00",
        "source": "App",
        "text": "hello",
    }
