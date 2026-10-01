"""Tests for the memory text non-Claude harnesses get: the protocol, filled in, and the index cut as Claude cuts it."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "agent_memory_context.py"
_spec = importlib.util.spec_from_file_location("agent_memory_context", _SCRIPT)
assert _spec is not None and _spec.loader is not None
memory_context = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(memory_context)

INDEX_MAX_BYTES = memory_context.INDEX_MAX_BYTES
INDEX_MAX_LINES = memory_context.INDEX_MAX_LINES
PROTOCOL_PATH = memory_context.PROTOCOL_PATH
main = memory_context.main
render_memory_context = memory_context.render_memory_context
truncate_index = memory_context.truncate_index
latest_changes = memory_context.latest_changes
render_changes_notice = memory_context.render_changes_notice
claude_hook_output = memory_context.claude_hook_output
stamp_note_text = memory_context.stamp_note_text
stamp_note = memory_context.stamp_note
CHANGE_MAX_AGE = memory_context.CHANGE_MAX_AGE

_NOW = datetime(2026, 10, 1, 18, 7, 56, tzinfo=timezone.utc)


def _notes_dir(tmp_path: Path, index: str | None) -> Path:
    notes_dir = tmp_path / "memories"
    notes_dir.mkdir()
    if index is not None:
        (notes_dir / "MEMORY.md").write_text(index)
    return notes_dir


def test_the_shipped_protocol_names_the_folder_the_harness_and_the_index() -> None:
    rendered = render_memory_context(
        PROTOCOL_PATH.read_text(),
        None,
        Path("/home/user/workspace/data/memories"),
        "pi-coding",
    )

    assert (
        "persistent, file-based memory at `/home/user/workspace/data/memories/`"
        in rendered
    )
    assert "  source: pi-coding\n---" in rendered
    assert (
        "is filled in for you each time you save; don't write it yourself." in rendered
    )
    assert "`/home/user/workspace/data/memories/MEMORY.md`" in rendered
    assert "{notes_dir}" not in rendered
    assert "{harness}" not in rendered


def test_the_index_follows_the_protocol() -> None:
    rendered = render_memory_context(
        "Protocol.",
        "- [Units](units.md) — Prefers metric\n",
        Path("/n"),
        "pi-coding",
    )

    assert rendered == (
        "Protocol.\n\n## Your memory index\n\n"
        "The current contents of /n/MEMORY.md (open a note's file when it looks relevant):\n\n"
        "- [Units](units.md) — Prefers metric\n"
    )


@pytest.mark.parametrize("index", [None, "", "  \n\n"])
def test_a_missing_or_empty_index_says_nothing_is_saved_yet(index: str | None) -> None:
    rendered = render_memory_context("Protocol.", index, Path("/n"), "pi-coding")

    assert (
        rendered
        == "Protocol.\n\n## Your memory index\n\n/n/MEMORY.md is empty: nothing has been saved yet.\n"
    )


def test_the_index_is_cut_at_200_lines() -> None:
    index = "".join(
        f"- [Note {idx}](note-{idx}.md) — hook\n" for idx in range(INDEX_MAX_LINES + 50)
    )

    kept = truncate_index(index).splitlines()

    assert len(kept) == INDEX_MAX_LINES
    assert (
        kept[-1]
        == f"- [Note {INDEX_MAX_LINES - 1}](note-{INDEX_MAX_LINES - 1}.md) — hook"
    )


def test_the_index_is_cut_at_25kb_on_a_line_boundary() -> None:
    long_line = "- [Note](note.md) — " + "x" * 1000
    index = "\n".join([long_line] * 40)

    kept = truncate_index(index)

    assert len(kept.encode("utf-8")) <= INDEX_MAX_BYTES
    assert all(line == long_line for line in kept.splitlines())
    assert len(kept.splitlines()) == INDEX_MAX_BYTES // (len(long_line) + 1)


def test_main_prints_the_protocol_and_the_live_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    notes_dir = _notes_dir(tmp_path, "- [Units](units.md) — Prefers metric\n")
    protocol = tmp_path / "protocol.md"
    protocol.write_text("Keep notes in {notes_dir} as {harness}.")

    assert (
        main(
            [
                "--harness",
                "pi-coding",
                "--notes-dir",
                str(notes_dir),
                "--protocol",
                str(protocol),
                "--changes",
                str(tmp_path / "user-changes.jsonl"),
            ]
        )
        == 0
    )

    out = capsys.readouterr().out
    assert out.startswith(
        f"Keep notes in {notes_dir} as pi-coding.\n\n## Your memory index\n"
    )
    assert out.endswith("- [Units](units.md) — Prefers metric\n")


def test_main_prints_nothing_and_succeeds_when_the_protocol_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    notes_dir = _notes_dir(tmp_path, "- [Units](units.md) — x\n")

    exit_code = main(
        [
            "--harness",
            "pi-coding",
            "--notes-dir",
            str(notes_dir),
            "--protocol",
            str(tmp_path / "missing.md"),
            "--changes",
            str(tmp_path / "user-changes.jsonl"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert "cannot read" in captured.err


def test_the_script_runs_under_a_plain_python3_with_the_home_folders_notes(
    tmp_path: Path,
) -> None:
    notes_dir = tmp_path / "workspace" / "data" / "memories"
    notes_dir.mkdir(parents=True)
    (notes_dir / "MEMORY.md").write_text("- [Role](role.md) — Is a designer\n")

    result = subprocess.run(
        [sys.executable, "-I", str(_SCRIPT), "--harness", "pi-coding"],
        capture_output=True,
        text=True,
        timeout=30,
        env={"HOME": str(tmp_path)},
    )

    assert result.returncode == 0, result.stderr
    assert f"memory at `{notes_dir}/`" in result.stdout
    assert result.stdout.endswith("- [Role](role.md) — Is a designer\n")


def test_json_keeps_the_fixed_protocol_apart_from_the_index_and_notices(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    notes_dir = _notes_dir(tmp_path, "- [Units](units.md) — Prefers metric\n")
    protocol = tmp_path / "protocol.md"
    protocol.write_text("Keep notes in {notes_dir} as {harness}.")
    changes = tmp_path / "user-changes.jsonl"
    changes.write_text(_change("profile.md", "DELETED", datetime.now(timezone.utc)))
    argv = [
        "--harness",
        "pi-coding",
        "--notes-dir",
        str(notes_dir),
        "--protocol",
        str(protocol),
        "--changes",
        str(changes),
        "--json",
    ]

    main(argv)
    first = json.loads(capsys.readouterr().out)
    main(argv)
    second = json.loads(capsys.readouterr().out)

    assert first == second
    assert first["protocol"] == f"Keep notes in {notes_dir} as pi-coding."
    assert first["memory"].startswith("## Your memory index")
    assert "- [Units](units.md) — Prefers metric" in first["memory"]
    assert "`profile.md` was deleted" in first["memory"]
    assert "## Your memory index" not in first["protocol"]


def test_stamping_sets_modified_and_adds_source_inside_metadata() -> None:
    note = "---\nname: job\ndescription: engineer\nmetadata:\n  type: user\n---\n\nx\n"

    assert stamp_note_text(note, "pi-coding", _NOW) == (
        "---\nname: job\ndescription: engineer\nmetadata:\n  type: user\n"
        "  source: pi-coding\n  modified: 2026-10-01T18:07:56Z\n---\n\nx\n"
    )


def test_stamping_replaces_a_guessed_date_and_keeps_an_existing_source() -> None:
    note = (
        "---\nname: job\nmodified: 2025-06-18T00:00:00Z\nmetadata:\n  type: user\n  source: codex\n"
        "  modified: 2025-06-18T00:00:00Z\n  node_type: memory\n---\nx\n"
    )

    assert stamp_note_text(note, "pi-coding", _NOW) == (
        "---\nname: job\nmetadata:\n  type: user\n  source: codex\n  node_type: memory\n"
        "  modified: 2026-10-01T18:07:56Z\n---\nx\n"
    )


def test_stamping_adds_a_metadata_block_when_there_is_none_and_skips_notes_without_frontmatter() -> (
    None
):
    assert stamp_note_text("---\nname: job\n---\nx\n", "pi-coding", _NOW) == (
        "---\nname: job\nmetadata:\n  source: pi-coding\n  modified: 2026-10-01T18:07:56Z\n---\nx\n"
    )
    assert stamp_note_text("No frontmatter.\n", "pi-coding", _NOW) is None
    assert stamp_note_text("---\nname: never closed\n", "pi-coding", _NOW) is None


def test_stamp_note_rewrites_a_note_in_the_folder_and_nothing_else(
    tmp_path: Path,
) -> None:
    notes_dir = _notes_dir(tmp_path, "- [Job](job.md) — engineer\n")
    job = notes_dir / "job.md"
    job.write_text("---\nname: job\nmetadata:\n  type: user\n---\nx\n")
    job.chmod(0o640)
    outside = tmp_path / "job.md"
    outside.write_text("---\nname: job\n---\nx\n")
    index_before = (notes_dir / "MEMORY.md").read_text()

    assert stamp_note(job, notes_dir, "pi-coding", _NOW) is True
    assert stamp_note(outside, notes_dir, "pi-coding", _NOW) is False
    assert stamp_note(notes_dir / "MEMORY.md", notes_dir, "pi-coding", _NOW) is False
    assert stamp_note(notes_dir / "missing.md", notes_dir, "pi-coding", _NOW) is False

    assert "  modified: 2026-10-01T18:07:56Z\n" in job.read_text()
    assert oct(job.stat().st_mode & 0o777) == oct(0o640)
    assert outside.read_text() == "---\nname: job\n---\nx\n"
    assert (notes_dir / "MEMORY.md").read_text() == index_before
    assert sorted(path.name for path in notes_dir.iterdir()) == ["MEMORY.md", "job.md"]


def _change(file_name: str, change: str, at: datetime) -> str:
    return (
        f'{{"file_name":"{file_name}","change":"{change}",'
        f'"at":"{at.strftime("%Y-%m-%dT%H:%M:%SZ")}"}}\n'
    )


def test_each_notes_latest_change_is_kept_oldest_first_and_stale_or_broken_lines_skipped() -> (
    None
):
    text = (
        _change("units.md", "EDITED", _NOW - timedelta(hours=2))
        + _change("profile.md", "DELETED", _NOW - timedelta(hours=1))
        + _change("units.md", "DELETED", _NOW)
        + _change("ancient.md", "DELETED", _NOW - CHANGE_MAX_AGE - timedelta(minutes=1))
        + _change("renamed.md", "RENAMED", _NOW)
        + "not json\n"
        + '{"file_name": "no-time.md", "change": "DELETED"}\n'
        + '{"file_name": "naive.md", "change": "DELETED", "at": "2026-10-01T22:00:00"}\n'
    )

    assert latest_changes(text, _NOW) == [
        ("profile.md", "deleted", _NOW - timedelta(hours=1)),
        ("units.md", "deleted", _NOW),
    ]


def test_the_notice_tells_chats_not_to_restore_a_deleted_note_or_revert_an_edit() -> (
    None
):
    notice = render_changes_notice(
        [
            ("profile.md", "deleted", _NOW),
            ("units.md", "edited", _NOW + timedelta(minutes=5)),
        ]
    )

    assert notice.startswith("## Changes the user made to saved memories\n")
    assert (
        "- `profile.md` was deleted 2026-10-01 18:07 UTC. Don't save what it said again, in that note or any other, "
        "unless the user tells you it again.\n"
    ) in notice
    assert (
        "- `units.md` was edited 2026-10-01 18:12 UTC. Read it again before you change it, "
        "and don't put back anything the user removed.\n"
    ) in notice
    assert render_changes_notice([]) == ""


def _hook_input(tmp_path: Path, session_id: str, started: datetime) -> str:
    transcript = tmp_path / f"{session_id}.jsonl"
    transcript.write_text(
        '{"type":"permission-mode"}\n'
        + json.dumps(
            {"type": "user", "timestamp": started.strftime("%Y-%m-%dT%H:%M:%S.000Z")}
        )
        + "\n"
    )
    return json.dumps({"session_id": session_id, "transcript_path": str(transcript)})


def _note(
    notes_dir: Path, name: str, description: str, at: datetime, extra: str = ""
) -> None:
    path = notes_dir / name
    path.write_text(
        f"---\nname: {name[:-3]}\ndescription: {description}\nmetadata:\n  type: user\n{extra}---\n\nx\n"
    )
    os.utime(path, (at.timestamp(), at.timestamp()))


def test_the_claude_hook_lists_notes_saved_since_the_chat_started_but_not_its_own(
    tmp_path: Path,
) -> None:
    notes_dir = _notes_dir(
        tmp_path,
        "- [Location](user-location.md) — lives in California\n- [Job](user-profession.md) — software engineer\n",
    )
    started = _NOW - timedelta(minutes=10)
    _note(
        notes_dir,
        "user-location.md",
        "lives in California",
        started - timedelta(minutes=1),
    )
    _note(
        notes_dir,
        "user-profession.md",
        "software engineer",
        started + timedelta(minutes=1),
    )
    _note(
        notes_dir,
        "own.md",
        "saved by this chat",
        started + timedelta(minutes=2),
        "  originSessionId: sess-1\n",
    )
    _note(notes_dir, "unindexed.md", "Plays the cello", started + timedelta(minutes=3))
    (notes_dir / "README.md").write_text("readme")

    out = claude_hook_output(
        _hook_input(tmp_path, "sess-1", started),
        notes_dir,
        tmp_path / "none.jsonl",
        _NOW,
    )

    assert out == (
        "## Notes saved since this chat started\n\n"
        "This chat loaded the memory index when it started. Other chats (or the user) saved or changed these notes "
        "since then, so they are not in that index; open a note's file when it looks relevant:\n"
        "- [Job](user-profession.md) — software engineer\n"
        "- `unindexed.md` — Plays the cello\n"
    )


def test_the_claude_hook_puts_the_change_notice_first_and_says_nothing_when_nothing_is_new(
    tmp_path: Path,
) -> None:
    notes_dir = _notes_dir(tmp_path, None)
    started = _NOW - timedelta(minutes=10)
    changes = tmp_path / "user-changes.jsonl"

    assert (
        claude_hook_output(
            _hook_input(tmp_path, "s", started), notes_dir, changes, _NOW
        )
        == ""
    )

    changes.write_text(
        _change("user-profile.md", "DELETED", _NOW - timedelta(minutes=5))
    )
    _note(notes_dir, "job.md", "software engineer", started + timedelta(minutes=1))
    out = claude_hook_output(
        _hook_input(tmp_path, "s", started), notes_dir, changes, _NOW
    )
    assert out.index("## Changes the user made") < out.index(
        "## Notes saved since this chat started"
    )


@pytest.mark.parametrize(
    "hook_input",
    ["", "not json", '{"session_id": "s"}', '{"transcript_path": "/missing.jsonl"}'],
)
def test_the_claude_hook_without_a_readable_transcript_gives_only_the_change_notice(
    tmp_path: Path, hook_input: str
) -> None:
    notes_dir = _notes_dir(tmp_path, None)
    _note(notes_dir, "job.md", "software engineer", _NOW)
    changes = tmp_path / "user-changes.jsonl"
    changes.write_text(_change("user-profile.md", "DELETED", _NOW))

    out = claude_hook_output(hook_input, notes_dir, changes, _NOW)

    assert out.startswith("## Changes the user made to saved memories")
    assert "Notes saved since" not in out


def test_the_claude_hook_runs_from_stdin_under_a_plain_python3(tmp_path: Path) -> None:
    notes_dir = tmp_path / "workspace" / "data" / "memories"
    notes_dir.mkdir(parents=True)
    started = datetime.now(timezone.utc) - timedelta(minutes=10)
    _note(notes_dir, "job.md", "software engineer", datetime.now(timezone.utc))

    result = subprocess.run(
        [sys.executable, "-I", str(_SCRIPT), "--claude-hook"],
        input=_hook_input(tmp_path, "s", started),
        capture_output=True,
        text=True,
        timeout=30,
        env={"HOME": str(tmp_path)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("- `job.md` — software engineer\n")


def test_the_full_context_ends_with_the_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    notes_dir = _notes_dir(tmp_path, "- [Units](units.md) — Prefers metric\n")
    protocol = tmp_path / "protocol.md"
    protocol.write_text("Protocol.")
    changes = tmp_path / "user-changes.jsonl"
    changes.write_text(_change("profile.md", "DELETED", datetime.now(timezone.utc)))

    main(
        [
            "--harness",
            "pi-coding",
            "--notes-dir",
            str(notes_dir),
            "--protocol",
            str(protocol),
            "--changes",
            str(changes),
        ]
    )

    out = capsys.readouterr().out
    assert out.index("- [Units](units.md) — Prefers metric") < out.index(
        "## Changes the user made to saved memories"
    )
    assert "`profile.md` was deleted" in out
