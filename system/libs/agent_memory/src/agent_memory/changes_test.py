"""The change log records what an agent's turn changed in its memory folder, as one undoable change."""

import json
import os
import time
from pathlib import Path

import pytest

from agent_memory import changes
from agent_memory.changes import ChangeLog
from agent_memory.changes import ChangedSinceError
from agent_memory.changes import format_agent_note
from agent_memory.changes import parse_agent_note
from agent_memory.changes import parse_agent_notes
from agent_memory.changes import replace_key_lines


@pytest.fixture
def log(tmp_path: Path) -> ChangeLog:
    (tmp_path / "memories").mkdir()
    return ChangeLog(changes_dir=tmp_path / "changes", memory_dir=tmp_path / "memories")


def _write(log: ChangeLog, name: str, text: str) -> dict:
    (log.memory_dir / name).write_text(text)
    return {"file_path": str(log.memory_dir / name)}


def _one(changes: list[dict]) -> dict:
    assert len(changes) == 1, changes
    return changes[0]


def _index(log: ChangeLog) -> str:
    return (log.memory_dir / "MEMORY.md").read_text()


def test_a_new_note_and_its_index_line_are_one_change_naming_both_tool_calls(
    log: ChangeLog,
) -> None:
    _write(log, "MEMORY.md", "# Index\n")
    log.start_turn("s1")
    log.record_tool_call(
        "s1", "t1", _write(log, "tea.md", "likes tea"), chat_id="chat-1"
    )
    change = _one(
        log.record_tool_call(
            "s1",
            "t2",
            _write(log, "MEMORY.md", "# Index\n- [Tea](tea.md) — likes tea\n"),
        )
    )
    assert change["summary"] == 'added the note "Tea" and its index line'
    assert change["note"] == "tea.md"
    assert change["tool_use_ids"] == ["t1", "t2"]
    assert change["chat_id"] == "chat-1"
    assert change["files"] == [
        {
            "name": "MEMORY.md",
            "part": "lines",
            "before": None,
            "after": "- [Tea](tea.md) — likes tea",
        },
        {"name": "tea.md", "before": None, "after": "likes tea"},
    ]
    assert [c["id"] for c in log.list_changes()] == [change["id"]]


def test_two_notes_in_one_reply_are_two_changes_each_with_its_own_index_line(
    log: ChangeLog,
) -> None:
    _write(log, "MEMORY.md", "# Index\n")
    log.start_turn("s1")
    log.record_tool_call("s1", "t1", _write(log, "tea.md", "likes tea"))
    log.record_tool_call("s1", "t2", _write(log, "coffee.md", "no coffee"))
    touched = log.record_tool_call(
        "s1",
        "t3",
        _write(
            log,
            "MEMORY.md",
            "# Index\n- [Tea](tea.md) — tea\n- [Coffee](coffee.md) — coffee\n",
        ),
    )
    assert sorted(c["summary"] for c in touched) == [
        'added the note "Coffee" and its index line',
        'added the note "Tea" and its index line',
    ]
    tea, coffee = sorted(log.list_changes(), key=lambda c: c["note"], reverse=True)
    assert tea["tool_use_ids"] == ["t1", "t3"] and coffee["tool_use_ids"] == [
        "t2",
        "t3",
    ]
    assert tea["files"][0]["after"] == "- [Tea](tea.md) — tea"
    assert coffee["files"][0]["after"] == "- [Coffee](coffee.md) — coffee"
    # Undoing one takes out its note and its line, and leaves the other's.
    log.undo(tea["id"])
    assert not (log.memory_dir / "tea.md").exists()
    assert (log.memory_dir / "coffee.md").exists()
    assert _index(log) == "# Index\n- [Coffee](coffee.md) — coffee\n"
    log.undo(coffee["id"])
    assert _index(log) == "# Index\n"


def test_editing_one_note_s_index_title_only_is_that_note_s_change(
    log: ChangeLog,
) -> None:
    _write(
        log,
        "MEMORY.md",
        "# Index\n- [Tea](tea.md) — tea\n- [Coffee](coffee.md) — coffee\n",
    )
    _write(log, "tea.md", "likes tea")
    log.start_turn("s1")
    change = _one(
        log.record_tool_call(
            "s1",
            "t1",
            _write(
                log,
                "MEMORY.md",
                "# Index\n- [Green tea](tea.md) — tea\n- [Coffee](coffee.md) — coffee\n",
            ),
        )
    )
    assert change["note"] == "tea.md"
    assert change["summary"] == 'changed the index line of "Tea"'
    assert change["files"] == [
        {
            "name": "MEMORY.md",
            "part": "lines",
            "before": "- [Tea](tea.md) — tea",
            "after": "- [Green tea](tea.md) — tea",
        }
    ]
    # The coffee line, edited after, is not this change's: undo puts back only the tea line.
    _write(
        log,
        "MEMORY.md",
        "# Index\n- [Green tea](tea.md) — tea\n- [Coffee](coffee.md) — none\n",
    )
    log.undo(change["id"])
    assert (
        _index(log) == "# Index\n- [Tea](tea.md) — tea\n- [Coffee](coffee.md) — none\n"
    )


def test_a_heading_only_index_edit_is_an_index_change_of_its_own(
    log: ChangeLog,
) -> None:
    _write(log, "MEMORY.md", "# Index\n- [Tea](tea.md) — tea\n")
    log.start_turn("s1")
    log.record_tool_call("s1", "t1", _write(log, "tea.md", "likes tea"))
    touched = log.record_tool_call(
        "s1",
        "t2",
        _write(log, "MEMORY.md", "# My notes\n- [Tea](tea.md) — tea\n- loose line\n"),
    )
    index_only = _one(touched)
    assert index_only["note"] is None
    assert index_only["summary"] == "changed the index"
    assert index_only["files"] == [
        {
            "name": "MEMORY.md",
            "part": "lines",
            "before": "# Index\n",
            "after": "# My notes\n- loose line\n",
        }
    ]
    assert len(log.list_changes()) == 2
    # A note added later keeps its line when the heading change is undone.
    _write(
        log,
        "MEMORY.md",
        "# My notes\n- [Tea](tea.md) — tea\n- loose line\n- [Cake](cake.md) — cake\n",
    )
    log.undo(index_only["id"])
    assert _index(log) == "# Index\n- [Tea](tea.md) — tea\n- [Cake](cake.md) — cake\n"


def test_undo_refuses_when_the_note_s_index_line_changed_since_unless_forced(
    log: ChangeLog,
) -> None:
    _write(log, "MEMORY.md", "# Index\n")
    log.start_turn("s1")
    log.record_tool_call("s1", "t1", _write(log, "tea.md", "likes tea"))
    change = _one(
        log.record_tool_call(
            "s1", "t2", _write(log, "MEMORY.md", "# Index\n- [Tea](tea.md) — tea\n")
        )
    )
    _write(log, "MEMORY.md", "# Index\n- [Tea](tea.md) — tea, green\n")
    with pytest.raises(ChangedSinceError) as refused:
        log.undo(change["id"])
    assert refused.value.names == ["MEMORY.md"]
    assert (log.memory_dir / "tea.md").exists()
    log.undo(change["id"], force=True)
    assert not (log.memory_dir / "tea.md").exists()
    assert _index(log) == "# Index\n"


def test_undo_puts_back_a_removed_index_line_where_it_stood(log: ChangeLog) -> None:
    _write(
        log,
        "MEMORY.md",
        "# Index\n- [Tea](tea.md) — tea\n- [Coffee](coffee.md) — coffee\n",
    )
    _write(log, "tea.md", "likes tea")
    log.start_turn("s1")
    (log.memory_dir / "tea.md").unlink()
    log.record_tool_call("s1", "t1", {"command": f"rm {log.memory_dir}/tea.md"})
    change = _one(
        log.record_tool_call(
            "s1",
            "t2",
            _write(log, "MEMORY.md", "# Index\n- [Coffee](coffee.md) — coffee\n"),
        )
    )
    assert change["summary"] == 'deleted the note "Tea" and its index line'
    log.undo(change["id"])
    assert (log.memory_dir / "tea.md").read_text() == "likes tea"
    assert (
        _index(log)
        == "# Index\n- [Coffee](coffee.md) — coffee\n- [Tea](tea.md) — tea\n"
    )


def test_a_record_from_before_the_split_is_undone_whole(log: ChangeLog) -> None:
    _write(log, "MEMORY.md", "# Index\n- [Tea](tea.md)\n")
    _write(log, "tea.md", "likes tea")
    log.changes_dir.mkdir(parents=True)
    old = {
        "id": "20261118T215743570540-06d576",
        "time": "2026-11-18T21:57:43+00:00",
        "session_id": "s0",
        "chat_id": "chat-1",
        "tool_use_ids": ["t1", "t2"],
        "files": [
            {
                "name": "MEMORY.md",
                "before": "# Index\n",
                "after": "# Index\n- [Tea](tea.md)\n",
            },
            {"name": "tea.md", "before": None, "after": "likes tea"},
        ],
        "undone_at": None,
    }
    (log.changes_dir / f"{old['id']}.json").write_text(json.dumps(old))
    change = log.get(old["id"])
    assert change["note"] is None
    assert change["summary"] == 'added the note "Tea" and its index line'
    log.undo(old["id"])
    assert _index(log) == "# Index\n"
    assert not (log.memory_dir / "tea.md").exists()


def test_two_edits_to_one_note_keep_the_text_from_before_the_first(
    log: ChangeLog,
) -> None:
    _write(log, "tea.md", "v1")
    log.start_turn("s1")
    log.record_tool_call("s1", "t1", _write(log, "tea.md", "v2"))
    change = _one(log.record_tool_call("s1", "t2", _write(log, "tea.md", "v3")))
    assert change["files"] == [{"name": "tea.md", "before": "v1", "after": "v3"}]
    assert change["summary"] == 'changed the note "Tea"'


def test_each_turn_gets_its_own_change(log: ChangeLog) -> None:
    _write(log, "tea.md", "v1")
    log.start_turn("s1")
    log.record_tool_call("s1", "t1", _write(log, "tea.md", "v2"))
    log.start_turn("s1")
    (log.memory_dir / "tea.md").unlink()
    log.record_tool_call("s1", "t2", {"command": f"rm {log.memory_dir}/tea.md"})
    first, second = log.list_changes()
    assert first["files"] == [{"name": "tea.md", "before": "v1", "after": "v2"}]
    assert second["files"] == [{"name": "tea.md", "before": "v2", "after": None}]
    assert second["summary"] == 'deleted the note "Tea"'


def test_a_turn_that_puts_things_back_leaves_nothing_to_undo(log: ChangeLog) -> None:
    _write(log, "tea.md", "v1")
    log.start_turn("s1")
    log.record_tool_call("s1", "t1", _write(log, "tea.md", "v2"))
    assert log.record_tool_call("s1", "t2", _write(log, "tea.md", "v1")) == []
    assert log.list_changes() == []


def test_a_call_that_does_not_name_the_folder_or_changes_nothing_records_nothing(
    log: ChangeLog,
) -> None:
    _write(log, "tea.md", "v1")
    log.start_turn("s1")
    assert (
        log.record_tool_call("s1", "t1", {"command": f"cat {log.memory_dir}/tea.md"})
        == []
    )
    assert log.record_tool_call("s1", "t2", {"command": "git status"}) == []
    assert log.list_changes() == []


def test_an_edit_the_user_made_between_turns_is_not_charged_to_the_agent(
    log: ChangeLog,
) -> None:
    _write(log, "tea.md", "v1")
    log.start_turn("s1")
    _write(log, "tea.md", "edited in the Memory app")
    log.start_turn("s1")
    assert (
        log.record_tool_call("s1", "t1", {"file_path": str(log.memory_dir / "tea.md")})
        == []
    )


def test_another_session_s_change_is_not_folded_into_this_one(log: ChangeLog) -> None:
    log.start_turn("a")
    log.start_turn("b")
    log.record_tool_call("a", "t1", _write(log, "tea.md", "tea"))
    log.record_tool_call("b", "t2", _write(log, "coffee.md", "coffee"))
    assert sorted(c["summary"] for c in log.list_changes()) == [
        'added the note "Coffee"',
        'added the note "Tea"',
    ]
    assert [c["summary"] for c in log.list_changes({"b"})] == [
        'added the note "Coffee"'
    ]


def test_undo_restores_the_files_and_is_not_then_charged_to_the_agent(
    log: ChangeLog,
) -> None:
    _write(log, "MEMORY.md", "# Index\n")
    log.start_turn("s1")
    log.start_turn("s2")
    log.record_tool_call("s1", "t1", _write(log, "tea.md", "tea"))
    change = _one(
        log.record_tool_call(
            "s1", "t2", _write(log, "MEMORY.md", "# Index\n- [Tea](tea.md)\n")
        )
    )
    undone = log.undo(change["id"])
    assert _index(log) == "# Index\n"
    assert not (log.memory_dir / "tea.md").exists()
    assert undone["undone_at"] is not None
    assert log.record_tool_call("s1", "t3", {"command": f"ls {log.memory_dir}"}) == []
    assert log.record_tool_call("s2", "t4", {"command": f"ls {log.memory_dir}"}) == []
    assert len(log.list_changes()) == 1


def test_undo_refuses_when_a_file_changed_again_unless_forced(log: ChangeLog) -> None:
    _write(log, "tea.md", "v1")
    log.start_turn("s1")
    change = _one(log.record_tool_call("s1", "t1", _write(log, "tea.md", "v2")))
    _write(log, "tea.md", "edited later")
    with pytest.raises(ChangedSinceError):
        log.undo(change["id"])
    assert (log.memory_dir / "tea.md").read_text() == "edited later"
    log.undo(change["id"], force=True)
    assert (log.memory_dir / "tea.md").read_text() == "v1"


def test_undo_brings_back_a_deleted_note(log: ChangeLog) -> None:
    _write(log, "tea.md", "v1")
    log.start_turn("s1")
    (log.memory_dir / "tea.md").unlink()
    change = _one(
        log.record_tool_call("s1", "t1", {"command": f"rm {log.memory_dir}/tea.md"})
    )
    log.undo(change["id"])
    assert (log.memory_dir / "tea.md").read_text() == "v1"


def test_replacing_a_key_s_lines_keeps_every_other_line() -> None:
    text = "# Index\n- [A](a.md) — 1\n- [B](b.md)\n- [A](a.md) — 2\n"
    assert (
        replace_key_lines(text, "a.md", ["- [A](a.md) — 1", "- [A](a.md) — 2"], [])
        == "# Index\n- [B](b.md)\n"
    )
    assert replace_key_lines(
        text, "a.md", ["- [A](a.md) — 1", "- [A](a.md) — 2"], ["- [A](a.md) — 3"]
    ) == ("# Index\n- [A](a.md) — 3\n- [B](b.md)\n")
    assert (
        replace_key_lines("# Index\n", "c.md", [], ["- [C](c.md)"])
        == "# Index\n- [C](c.md)\n"
    )
    assert replace_key_lines(None, "c.md", [], ["- [C](c.md)"]) == "- [C](c.md)"


def test_a_hook_context_naming_several_changes_reads_back_one_per_line() -> None:
    text = "\n".join(
        format_agent_note({"id": i, "summary": s})
        for i, s in (("c1", 'added the note "Tea"'), ("c2", "changed the index"))
    )
    assert [(i, s) for i, s, _ in parse_agent_notes(text)] == [
        ("c1", 'added the note "Tea"'),
        ("c2", "changed the index"),
    ]
    assert parse_agent_notes("nothing here") == []


def test_the_agent_note_reads_back_as_the_change_it_names() -> None:
    note = format_agent_note(
        {
            "id": "20261118T1-abc123",
            "summary": 'added the note "Tea" and its index line',
        }
    )
    assert parse_agent_note(note) == (
        "20261118T1-abc123",
        'added the note "Tea" and its index line',
    )
    assert parse_agent_note("MEMORY CHANGED: something else") is None


def test_a_session_idle_past_the_cutoff_loses_its_bookkeeping_and_starts_afresh(
    log: ChangeLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    log.start_turn("old")
    old_files = [log.state_dir / "baseline-old.json", log.state_dir / "turn-old.json"]
    stale = time.time() - (changes.STALE_SESSION_DAYS + 1) * 86400
    for path in old_files:
        os.utime(path, (stale, stale))
    log.start_turn("new")
    assert not any(path.exists() for path in old_files)
    assert (log.state_dir / "baseline-new.json").exists()
    # The old session coming back records its changes as before, from its next turn.
    log.start_turn("old")
    assert log.record_tool_call("old", "t1", _write(log, "tea.md", "likes tea")) != []


def test_the_log_keeps_only_the_newest_records(
    log: ChangeLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(changes, "MAX_CHANGE_RECORDS", 2)
    ids = []
    for k in range(3):
        log.start_turn("s1")
        change = _one(
            log.record_tool_call("s1", f"t{k}", _write(log, f"note{k}.md", "text"))
        )
        ids.append(change["id"])
        # A second call in the same turn updates its record without evicting anything.
        assert (
            _one(
                log.record_tool_call(
                    "s1", f"t{k}b", _write(log, f"note{k}.md", "text 2")
                )
            )["id"]
            == change["id"]
        )
    assert [c["id"] for c in log.list_changes()] == ids[1:]
