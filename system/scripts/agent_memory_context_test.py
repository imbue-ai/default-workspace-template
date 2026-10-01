"""Tests for the memory text non-Claude harnesses get: the protocol, filled in, and the index cut as Claude cuts it."""

from __future__ import annotations

import importlib.util
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
        _NOW,
    )

    assert (
        "persistent, file-based memory at `/home/user/workspace/data/memories/`"
        in rendered
    )
    assert "  source: pi-coding\n" in rendered
    assert "`/home/user/workspace/data/memories/MEMORY.md`" in rendered
    assert (
        "  modified: <when you save it: it is 2026-10-01T18:07:56Z now>\n" in rendered
    )
    assert "{notes_dir}" not in rendered
    assert "{harness}" not in rendered
    assert "{now}" not in rendered


def test_the_index_follows_the_protocol() -> None:
    rendered = render_memory_context(
        "Protocol.",
        "- [Units](units.md) — Prefers metric\n",
        Path("/n"),
        "pi-coding",
        _NOW,
    )

    assert rendered == (
        "Protocol.\n\n## Your memory index\n\n"
        "The contents of /n/MEMORY.md as of this message (open a note's file when it looks relevant):\n\n"
        "- [Units](units.md) — Prefers metric\n"
    )


@pytest.mark.parametrize("index", [None, "", "  \n\n"])
def test_a_missing_or_empty_index_says_nothing_is_saved_yet(index: str | None) -> None:
    rendered = render_memory_context("Protocol.", index, Path("/n"), "pi-coding", _NOW)

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


def test_the_time_is_written_in_utc_whatever_zone_it_was_read_in() -> None:
    lisbon_summer = timezone(timedelta(hours=1))

    rendered = render_memory_context(
        "Saved at {now}.", None, Path("/n"), "pi-coding", _NOW.astimezone(lisbon_summer)
    )

    assert rendered.startswith("Saved at 2026-10-01T18:07:56Z.")


def test_main_stamps_the_current_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    protocol = tmp_path / "protocol.md"
    protocol.write_text("{now}")
    before = datetime.now(timezone.utc).replace(microsecond=0)

    main(
        [
            "--harness",
            "pi-coding",
            "--notes-dir",
            str(tmp_path),
            "--protocol",
            str(protocol),
        ]
    )

    stamped = datetime.strptime(
        capsys.readouterr().out.splitlines()[0], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)
    assert before <= stamped <= datetime.now(timezone.utc)
