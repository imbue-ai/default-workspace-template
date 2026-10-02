"""Tests for the memory text non-Claude harnesses get: the protocol, filled in, and the index cut as Claude cuts it."""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
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
read_watermark = memory_context.read_watermark
write_watermark = memory_context.write_watermark
HOOK_MARK_SLACK = memory_context.HOOK_MARK_SLACK
SAVED_SINCE_MAX_NOTES = memory_context.SAVED_SINCE_MAX_NOTES
stamp_note_text = memory_context.stamp_note_text
stamp_note = memory_context.stamp_note
sync_index = memory_context.sync_index
CHANGE_MAX_AGE = memory_context.CHANGE_MAX_AGE

_NOW = datetime(2026, 10, 1, 18, 7, 56, tzinfo=timezone.utc)


def _notes_dir(tmp_path: Path, index: str | None) -> Path:
    """A notes folder holding ``index``, and a note for each line in it whose description is that line's summary, so
    the index sync every run makes leaves the index as written."""
    notes_dir = tmp_path / "memories"
    notes_dir.mkdir()
    if index is not None:
        (notes_dir / "MEMORY.md").write_text(index)
        for file_name, summary in re.findall(r"\]\(([^)/]+\.md)\) — (.*)", index):
            (notes_dir / file_name).write_text(
                f"---\nname: {file_name[:-3]}\ndescription: {summary}\n---\n\nx\n"
            )
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
    (notes_dir / "role.md").write_text(
        "---\nname: role\ndescription: Is a designer\n---\nx\n"
    )

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


def test_each_notes_latest_change_is_kept_oldest_first_and_stale_or_broken_lines_skipped(
    tmp_path: Path,
) -> None:
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

    assert latest_changes(text, _NOW, tmp_path) == [
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


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _hook_input(tmp_path: Path, records: list[dict[str, object]]) -> str:
    """Claude's UserPromptSubmit input, over a transcript holding ``records`` in order."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"type":"permission-mode"}\n'
        + "".join(json.dumps(record) + "\n" for record in records)
    )
    return json.dumps({"session_id": "session", "transcript_path": str(transcript)})


def _user(at: datetime, text: str = "hello") -> dict[str, object]:
    return {
        "type": "user",
        "timestamp": _stamp(at),
        "message": {"role": "user", "content": text},
    }


def _own_write(at: datetime, notes_dir: Path, name: str) -> dict[str, object]:
    return {
        "type": "assistant",
        "timestamp": _stamp(at),
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Write",
                    "input": {"file_path": str(notes_dir / name)},
                }
            ]
        },
    }


def _note(notes_dir: Path, name: str, description: str, at: datetime) -> None:
    path = notes_dir / name
    path.write_text(
        f"---\nname: {name[:-3]}\ndescription: {description}\nmetadata:\n  type: user\n---\n\nx\n"
    )
    os.utime(path, (at.timestamp(), at.timestamp()))


def test_the_claude_hook_lists_notes_others_saved_since_the_chat_started(
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
    _note(notes_dir, "own.md", "saved by this chat", started + timedelta(minutes=2))
    _note(notes_dir, "unindexed.md", "Plays the cello", started + timedelta(minutes=3))
    (notes_dir / "README.md").write_text("readme")
    hook_input = _hook_input(
        tmp_path,
        [
            _user(started),
            _own_write(started + timedelta(minutes=2), notes_dir, "own.md"),
        ],
    )

    out = claude_hook_output(
        hook_input, notes_dir, tmp_path / "none.jsonl", tmp_path / "state", _NOW
    )

    assert out == (
        "## Notes saved since this chat started\n\n"
        "This chat loaded the memory index when it started. Other chats (or the user) saved or changed these notes "
        "since then, so they are not in that index; open a note's file when it looks relevant:\n"
        "- [Job](user-profession.md) — software engineer\n"
        "- `unindexed.md` — Plays the cello\n"
    )


def test_a_note_this_chat_created_but_another_chat_changed_later_is_announced(
    tmp_path: Path,
) -> None:
    notes_dir = _notes_dir(tmp_path, None)
    started = _NOW - timedelta(minutes=10)
    _note(notes_dir, "job.md", "changed by a pi chat", started + timedelta(minutes=5))
    hook_input = _hook_input(
        tmp_path,
        [
            _user(started),
            _own_write(started + timedelta(minutes=1), notes_dir, "job.md"),
        ],
    )

    out = claude_hook_output(
        hook_input, notes_dir, tmp_path / "none.jsonl", tmp_path / "state", _NOW
    )

    assert "- `job.md` — changed by a pi chat" in out


def test_the_claude_hook_announces_only_what_is_newer_than_its_last_run_for_the_chat(
    tmp_path: Path,
) -> None:
    notes_dir = _notes_dir(tmp_path, None)
    state_dir = tmp_path / "state"
    started = _NOW - timedelta(minutes=30)
    last_run = _NOW - timedelta(minutes=10)
    write_watermark(state_dir, "session", last_run)
    _note(
        notes_dir,
        "heard.md",
        "announced on an earlier message",
        last_run - timedelta(minutes=5),
    )
    _note(
        notes_dir, "new.md", "saved since the last run", last_run + timedelta(minutes=5)
    )
    changes = tmp_path / "user-changes.jsonl"
    changes.write_text(
        _change("old.md", "DELETED", last_run - timedelta(minutes=5))
        + _change("fresh.md", "DELETED", last_run + timedelta(minutes=5))
    )
    hook_input = _hook_input(tmp_path, [_user(started)])

    out = claude_hook_output(hook_input, notes_dir, changes, state_dir, _NOW)

    assert "`fresh.md` was deleted" in out
    assert "old.md" not in out
    assert "- `new.md` — saved since the last run" in out
    assert "heard.md" not in out
    assert out.index("## Changes the user made") < out.index(
        "## Notes saved since this chat started"
    )
    assert read_watermark(state_dir, "session") == _NOW - HOOK_MARK_SLACK


def test_each_note_and_change_is_announced_once_per_chat(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path, None)
    state_dir = tmp_path / "state"
    started = _NOW - timedelta(minutes=30)
    _note(notes_dir, "job.md", "software engineer", started + timedelta(minutes=1))
    changes = tmp_path / "user-changes.jsonl"
    changes.write_text(
        _change("user-profile.md", "DELETED", started + timedelta(minutes=1))
    )
    hook_input = _hook_input(tmp_path, [_user(started)])
    other_chat = json.dumps(
        {"session_id": "other", "transcript_path": str(tmp_path / "session.jsonl")}
    )

    first = claude_hook_output(hook_input, notes_dir, changes, state_dir, _NOW)
    second = claude_hook_output(
        hook_input, notes_dir, changes, state_dir, _NOW + timedelta(minutes=1)
    )
    other = claude_hook_output(
        other_chat, notes_dir, changes, state_dir, _NOW + timedelta(minutes=1)
    )

    assert (
        "`user-profile.md` was deleted" in first
        and "- `job.md` — software engineer" in first
    )
    assert second == ""
    assert other == first


def test_without_a_state_dir_the_hook_measures_from_the_chats_start(
    tmp_path: Path,
) -> None:
    notes_dir = _notes_dir(tmp_path, None)
    started = _NOW - timedelta(minutes=30)
    _note(notes_dir, "job.md", "software engineer", started + timedelta(minutes=1))
    hook_input = _hook_input(tmp_path, [_user(started)])

    for _ in range(2):
        assert "- `job.md` — software engineer" in claude_hook_output(
            hook_input, notes_dir, tmp_path / "none.jsonl", None, _NOW
        )


def test_an_unreadable_watermark_counts_as_none(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    write_watermark(state_dir, "../escape", _NOW)
    (state_dir / "memory-hook-broken.json").write_text("not json")

    assert read_watermark(state_dir, "../escape") == _NOW
    assert [
        path.name
        for path in state_dir.iterdir()
        if path.name.startswith("memory-hook-___")
    ] == ["memory-hook-___escape.json"]
    assert read_watermark(state_dir, "broken") is None
    assert read_watermark(state_dir, "") is None
    assert read_watermark(None, "session") is None


@pytest.mark.parametrize(
    "hook_input",
    ["", "not json", '{"session_id": "s"}', '{"transcript_path": "/missing.jsonl"}'],
)
def test_without_a_readable_transcript_the_hook_still_gives_the_change_notice(
    tmp_path: Path, hook_input: str
) -> None:
    notes_dir = _notes_dir(tmp_path, None)
    _note(notes_dir, "job.md", "software engineer", _NOW)
    changes = tmp_path / "user-changes.jsonl"
    changes.write_text(_change("user-profile.md", "DELETED", _NOW))

    out = claude_hook_output(hook_input, notes_dir, changes, tmp_path / "state", _NOW)

    assert out.startswith("## Changes the user made to saved memories")
    assert "Notes saved since" not in out


def test_the_claude_hook_runs_from_stdin_under_a_plain_python3(tmp_path: Path) -> None:
    notes_dir = tmp_path / "workspace" / "data" / "memories"
    notes_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    _note(notes_dir, "job.md", "software engineer", now - timedelta(minutes=1))

    hook_input = _hook_input(tmp_path, [_user(now - timedelta(minutes=10))])
    env = {"HOME": str(tmp_path), "MNGR_AGENT_STATE_DIR": str(tmp_path / "state")}

    def run_hook() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(_SCRIPT), "--claude-hook"],
            input=hook_input,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

    first = run_hook()
    second = run_hook()

    assert first.returncode == 0, first.stderr
    assert first.stdout.endswith("- [Job](job.md) — software engineer\n")
    assert (second.returncode, second.stdout) == (0, "")


def test_stamping_keeps_crlf_line_endings_and_leaves_inline_metadata_and_other_keys_alone() -> (
    None
):
    crlf = "---\r\nname: job\r\nmetadata:\r\n  type: user\r\n---\r\nx\r\n"
    inline = "---\nname: job\nmetadata: {type: user}\n---\nx\n"
    block_scalar = "---\nname: job\nnotes: |\n  modified: part of the text\nmetadata:\n  type: user\n---\nx\n"

    stamped_crlf = stamp_note_text(crlf, "pi-coding", _NOW)
    assert stamped_crlf is not None
    assert "\r\n  modified: 2026-10-01T18:07:56Z\r\n---\r\n" in stamped_crlf
    assert "\n" not in stamped_crlf.replace("\r\n", "")
    assert stamp_note_text(inline, "pi-coding", _NOW) is None
    assert "  modified: part of the text\n" in (
        stamp_note_text(block_scalar, "pi-coding", _NOW) or ""
    )


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


_CALIFORNIA = (
    "---\nname: user-location\n"
    'description: "User lives in California (US \\"Pacific\\" time)"\n'
    "metadata:\n  type: user\n---\n\nThe user lives in California.\n"
)


def test_sync_sets_a_stale_line_to_what_the_note_now_says(tmp_path: Path) -> None:
    notes_dir = _notes_dir(
        tmp_path,
        "- [User location](user-location.md) — lives in Virginia\n",
    )
    (notes_dir / "user-location.md").write_text(_CALIFORNIA)

    (notes_dir / "MEMORY.md").chmod(0o640)

    assert sync_index(notes_dir) is True
    assert (notes_dir / "MEMORY.md").read_text() == (
        '- [User location](user-location.md) — User lives in California (US "Pacific" time)\n'
    )
    assert (notes_dir / "MEMORY.md").stat().st_mode & 0o777 == 0o640


def test_sync_adds_missing_lines_drops_repeats_and_gone_notes_and_keeps_everything_else(
    tmp_path: Path,
) -> None:
    notes_dir = _notes_dir(
        tmp_path,
        "# Memory\n\nSome prose.\n"
        "- [Units](units.md) — metric\n"
        "- [Gone](gone.md) — a note whose file is gone\n"
        "- [Guide](docs/guide.md) — not a note in this folder\n"
        "- [Folded](folded.md) — kept as written\n"
        "* [Units again](units.md) - repeated\n",
    )
    (notes_dir / "gone.md").unlink()
    (notes_dir / "units.md").write_text(
        "---\nname: units\ndescription: Prefers metric units\n---\nx\n"
    )
    (notes_dir / "work_style.md").write_text(
        "---\nname: work_style\ndescription: 'Likes it''s short'\n---\nx\n"
    )
    (notes_dir / "no-summary.md").write_text("no frontmatter at all\n")
    (notes_dir / "folded.md").write_text(
        "---\nname: folded\ndescription: >-\n  spans lines\n---\nx\n"
    )
    (notes_dir / "README.md").write_text("---\ndescription: not a note\n---\n")

    sync_index(notes_dir)

    assert (notes_dir / "MEMORY.md").read_text() == (
        "# Memory\n\nSome prose.\n"
        "- [Units](units.md) — Prefers metric units\n"
        "- [Guide](docs/guide.md) — not a note in this folder\n"
        "- [Folded](folded.md) — kept as written\n"
        "- [Work style](work_style.md) — Likes it's short\n"
    )


def test_sync_leaves_an_index_that_already_matches_alone(tmp_path: Path) -> None:
    index = "- [Units](units.md) — Prefers metric units\r\n"
    notes_dir = _notes_dir(tmp_path, index)
    (notes_dir / "units.md").write_text(
        "---\nname: units\ndescription: Prefers metric units\n---\nx\n"
    )
    os.utime(notes_dir / "MEMORY.md", (1, 1))

    assert sync_index(notes_dir) is False
    assert (notes_dir / "MEMORY.md").read_bytes() == index.encode()
    assert (notes_dir / "MEMORY.md").stat().st_mtime == 1


def test_sync_writes_the_index_when_there_is_none_and_nothing_when_there_are_no_notes(
    tmp_path: Path,
) -> None:
    empty = _notes_dir(tmp_path, None)
    assert sync_index(empty) is False
    assert not (empty / "MEMORY.md").exists()

    (empty / "user-location.md").write_text(_CALIFORNIA)
    assert sync_index(empty) is True
    assert (
        (empty / "MEMORY.md")
        .read_text()
        .startswith("- [User location](user-location.md) — User lives in California")
    )


def test_stamping_a_note_pi_rewrote_also_corrects_its_index_line(
    tmp_path: Path,
) -> None:
    notes_dir = _notes_dir(
        tmp_path, "- [User location](user-location.md) — lives in Virginia\n"
    )
    (notes_dir / "user-location.md").write_text(_CALIFORNIA)

    assert (
        main(
            [
                "--stamp",
                str(notes_dir / "user-location.md"),
                "--harness",
                "pi-coding",
                "--notes-dir",
                str(notes_dir),
            ]
        )
        == 0
    )

    assert "lives in California" in (notes_dir / "MEMORY.md").read_text()
    assert "Virginia" not in (notes_dir / "MEMORY.md").read_text()


def test_the_index_a_pi_turn_gets_is_synced_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    notes_dir = _notes_dir(
        tmp_path, "- [User location](user-location.md) — lives in Virginia\n"
    )
    (notes_dir / "user-location.md").write_text(_CALIFORNIA)

    main(
        [
            "--json",
            "--harness",
            "pi-coding",
            "--notes-dir",
            str(notes_dir),
            "--changes",
            str(tmp_path / "user-changes.jsonl"),
        ]
    )

    memory = json.loads(capsys.readouterr().out)["memory"]
    assert "User lives in California" in memory
    assert "Virginia" not in memory


def _claude_post_tool_use_command() -> str:
    settings = json.loads(
        (_SCRIPT.parents[2] / ".claude" / "settings.json").read_text()
    )
    (entry,) = settings["hooks"]["PostToolUse"]
    assert set(entry["matcher"].split("|")) == {"Write", "Edit", "MultiEdit", "Bash"}
    (hook,) = entry["hooks"]
    return hook["command"]


@pytest.mark.parametrize(
    ("tool_input", "is_synced"),
    [
        ({"file_path": "/home/user/workspace/data/memories/user-location.md"}, True),
        ({"command": "sed -i 's/x/y/' ~/workspace/data/memories/MEMORY.md"}, True),
        ({"command": "ls system/apps"}, False),
    ],
)
def test_claudes_post_tool_use_hook_syncs_the_index_only_after_touching_the_notes(
    tmp_path: Path, tool_input: dict[str, str], is_synced: bool
) -> None:
    notes_dir = tmp_path / "workspace" / "data" / "memories"
    notes_dir.mkdir(parents=True)
    (notes_dir / "MEMORY.md").write_text(
        "- [User location](user-location.md) — lives in Virginia\n"
    )
    (notes_dir / "user-location.md").write_text(_CALIFORNIA)

    result = subprocess.run(
        ["sh", "-c", _claude_post_tool_use_command()],
        input=json.dumps({"tool_name": "Write", "tool_input": tool_input}),
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ["PATH"],
            "MNGR_AGENT_WORK_DIR": str(_SCRIPT.parents[2]),
        },
    )

    assert (result.returncode, result.stdout) == (0, "")
    assert ("California" in (notes_dir / "MEMORY.md").read_text()) is is_synced


def test_a_deleted_notes_line_does_not_come_back_once_its_file_is_gone(
    tmp_path: Path,
) -> None:
    notes_dir = _notes_dir(
        tmp_path,
        "- [User location](user-location.md) — lives in Virginia\n"
        "- [Job](job.md) — software engineer\n",
    )
    (notes_dir / "user-location.md").unlink()

    sync_index(notes_dir)
    sync_index(notes_dir)

    assert (
        notes_dir / "MEMORY.md"
    ).read_text() == "- [Job](job.md) — software engineer\n"


def test_sync_keeps_crlf_line_endings_in_the_file_on_disk(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path, None)
    (notes_dir / "MEMORY.md").write_bytes(
        b"# Memory\r\n- [Units](units.md) \xe2\x80\x94 metric\r\n"
    )
    (notes_dir / "units.md").write_text(
        "---\nname: units\ndescription: Prefers metric\n---\nx\n"
    )

    sync_index(notes_dir)

    assert (notes_dir / "MEMORY.md").read_bytes() == (
        "# Memory\r\n- [Units](units.md) — Prefers metric\r\n".encode()
    )


def test_stamping_keeps_crlf_line_endings_in_the_file_on_disk(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path, None)
    note = notes_dir / "job.md"
    note.write_bytes(b"---\r\nname: job\r\nmetadata:\r\n  type: user\r\n---\r\nx\r\n")

    assert stamp_note(note, notes_dir, "pi-coding", _NOW) is True
    assert b"  modified: 2026-10-01T18:07:56Z\r\n" in note.read_bytes()
    assert b"\n" not in note.read_bytes().replace(b"\r\n", b"")


def test_sync_waits_for_the_index_lock_the_app_takes(tmp_path: Path) -> None:
    notes_dir = _notes_dir(tmp_path, "- [Job](job.md) — software engineer\n")
    (notes_dir / "job.md").write_text("---\nname: job\ndescription: Engineer\n---\nx\n")
    with (notes_dir / ".MEMORY.md.lock").open("a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        sync = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(_SCRIPT),
                "--sync-index",
                "--notes-dir",
                str(notes_dir),
            ]
        )
        with pytest.raises(subprocess.TimeoutExpired):
            sync.wait(timeout=1)
        assert "software engineer" in (notes_dir / "MEMORY.md").read_text()
    assert sync.wait(timeout=30) == 0
    assert (notes_dir / "MEMORY.md").read_text() == "- [Job](job.md) — Engineer\n"


def test_the_claude_hook_tells_a_chat_about_every_delete_and_counts_notes_beyond_its_list(
    tmp_path: Path,
) -> None:
    notes_dir = _notes_dir(tmp_path, None)
    started = _NOW - timedelta(hours=1)
    changes = tmp_path / "user-changes.jsonl"
    changes.write_text(
        "".join(
            _change(f"n{idx:02}.md", "DELETED", started + timedelta(minutes=idx + 1))
            for idx in range(25)
        )
    )
    for idx in range(SAVED_SINCE_MAX_NOTES + 3):
        _note(
            notes_dir,
            f"s{idx:02}.md",
            f"saved {idx}",
            started + timedelta(seconds=idx + 1),
        )

    out = claude_hook_output(
        _hook_input(tmp_path, [_user(started)]),
        notes_dir,
        changes,
        tmp_path / "state",
        _NOW,
    )

    assert all(f"`n{idx:02}.md` was deleted" in out for idx in range(25))
    assert out.count("— saved ") == SAVED_SINCE_MAX_NOTES
    assert f"- and 3 more; read `{notes_dir / 'MEMORY.md'}` for the full list" in out


def test_a_delete_is_no_longer_announced_once_the_note_is_saved_again(
    tmp_path: Path,
) -> None:
    notes_dir = _notes_dir(tmp_path, None)
    text = _change("job.md", "DELETED", _NOW - timedelta(hours=1)) + _change(
        "home.md", "DELETED", _NOW - timedelta(hours=1)
    )
    _note(notes_dir, "job.md", "saved again", _NOW - timedelta(minutes=5))
    _note(notes_dir, "home.md", "older copy", _NOW - timedelta(hours=2))

    assert [name for name, _, _ in latest_changes(text, _NOW, notes_dir)] == ["home.md"]
