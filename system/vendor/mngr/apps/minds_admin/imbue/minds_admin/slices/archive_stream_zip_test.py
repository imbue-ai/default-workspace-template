import io
import json
import os
import random
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pytest

from imbue.minds_admin.slices.archive_stream_zip import MAX_REPORTED_SKIPS
from imbue.minds_admin.slices.archive_stream_zip import MSDOS_DIRECTORY_FLAG
from imbue.minds_admin.slices.archive_stream_zip import SUMMARY_MARKER
from imbue.minds_admin.slices.archive_stream_zip import WRITTEN
from imbue.minds_admin.slices.archive_stream_zip import empty_totals
from imbue.minds_admin.slices.archive_stream_zip import is_excluded
from imbue.minds_admin.slices.archive_stream_zip import skipped
from imbue.minds_admin.slices.archive_stream_zip import summary_json
from imbue.minds_admin.slices.archive_stream_zip import with_outcome

_SCRIPT_PATH = Path(__file__).parent / "archive_stream_zip.py"


def _run_streamer(arguments: list[str]) -> tuple[bytes, dict[str, Any]]:
    """Run the streamer as the VM would (a subprocess whose stdout is a pipe) and parse its summary."""
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), *arguments], capture_output=True, check=True, timeout=30
    )
    summary_lines = [line for line in completed.stderr.decode().splitlines() if line.startswith(SUMMARY_MARKER)]
    assert len(summary_lines) == 1, completed.stderr.decode()
    return completed.stdout, json.loads(summary_lines[0].removeprefix(SUMMARY_MARKER).strip())


def test_is_excluded_matches_double_star_patterns_on_any_component_run() -> None:
    excludes = ("**/.venv", "**/.cargo/registry", "top-level-only")
    assert is_excluded(".venv", excludes)
    assert is_excluded("a/b/.venv", excludes)
    assert is_excluded("home/.cargo/registry", excludes)
    assert not is_excluded("home/.cargo/bin", excludes)
    assert not is_excluded("a/.venv-notes", excludes)
    assert is_excluded("top-level-only", excludes)
    assert not is_excluded("nested/top-level-only", excludes)


def test_totals_count_every_skip_but_name_only_the_first_few() -> None:
    totals = with_outcome(with_outcome(empty_totals(), "a/dir/", WRITTEN), "a/f", (1, 7, None))
    for index in range(MAX_REPORTED_SKIPS + 3):
        totals = with_outcome(totals, f"a/special-{index}", skipped("fifo"))
    summary = json.loads(summary_json(totals))
    assert summary["entry_count"] == 2
    assert summary["entry_bytes"] == 7
    assert summary["skipped_count"] == MAX_REPORTED_SKIPS + 3
    assert len(summary["skipped_paths"]) == MAX_REPORTED_SKIPS
    assert summary["skipped_paths"][0] == "a/special-0: fifo"


def test_streamer_writes_a_streamable_zip_with_both_roots_excludes_symlinks_and_the_readme(tmp_path: Path) -> None:
    volume = tmp_path / "volume"
    (volume / "home" / "workspace" / ".venv").mkdir(parents=True)
    (volume / "home" / "workspace" / "file.txt").write_bytes(b"hello")
    (volume / "home" / "workspace" / ".venv" / "cached").write_bytes(b"cache")
    os.symlink("file.txt", volume / "home" / "workspace" / "link")
    layer = tmp_path / "layer"
    (layer / "var" / "log" / "mngr").mkdir(parents=True)
    (layer / "var" / "log" / "mngr" / "a.log").write_bytes(b"log")
    os.mkfifo(layer / "pipe")
    readme = tmp_path / "README.txt"
    readme.write_text("about this archive\n")

    stdout, summary = _run_streamer(
        [
            "--root",
            f"volume={volume}",
            "--root",
            f"container-layer={layer}",
            "--exclude",
            "**/.venv",
            "--readme",
            str(readme),
        ]
    )

    archive = zipfile.ZipFile(io.BytesIO(stdout))
    assert archive.testzip() is None
    names = [info.filename for info in archive.infolist()]
    assert names == [
        "README.txt",
        "volume/home/",
        "volume/home/workspace/",
        "volume/home/workspace/file.txt",
        "volume/home/workspace/link",
        "container-layer/var/",
        "container-layer/var/log/",
        "container-layer/var/log/mngr/",
        "container-layer/var/log/mngr/a.log",
    ]
    assert archive.read("volume/home/workspace/file.txt") == b"hello"
    assert archive.read("README.txt") == b"about this archive\n"
    # The symlink is stored as a symlink (its target as content, the link type in its mode).
    link_info = archive.getinfo("volume/home/workspace/link")
    assert archive.read(link_info) == b"file.txt"
    assert (link_info.external_attr >> 16) & 0o170000 == 0o120000
    # A directory carries the DOS directory attribute beside its Unix mode, as the stdlib writes it; a file does not.
    directory_info = archive.getinfo("volume/home/workspace/")
    assert directory_info.is_dir()
    assert directory_info.external_attr & MSDOS_DIRECTORY_FLAG
    assert (directory_info.external_attr >> 16) & 0o170000 == 0o040000
    assert not archive.getinfo("volume/home/workspace/file.txt").external_attr & MSDOS_DIRECTORY_FLAG
    assert summary["entry_count"] == len(names)
    assert summary["entry_bytes"] == len(b"hello") + len(b"log")
    assert summary["skipped_count"] == 1
    assert summary["skipped_paths"] == ["container-layer/pipe: not a regular file, directory or symlink"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can list any directory, so no listing failure can be provoked")
def test_streamer_names_a_directory_it_could_not_list_instead_of_dropping_it_silently(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "open").mkdir(parents=True)
    (root / "open" / "f").write_bytes(b"x")
    locked = root / "locked"
    locked.mkdir()
    (locked / "hidden").write_bytes(b"y")
    locked.chmod(0)
    try:
        stdout, summary = _run_streamer(["--root", f"a={root}"])
    finally:
        locked.chmod(0o700)

    # The directory itself is in the zip (its parent listed it); its contents are not, and the summary says so.
    assert [info.filename for info in zipfile.ZipFile(io.BytesIO(stdout)).infolist()] == [
        "a/locked/",
        "a/open/",
        "a/open/f",
    ]
    assert summary["skipped_count"] == 1
    assert summary["skipped_paths"] == ["a/locked: listing failed: Permission denied"]


def test_streamer_reports_a_missing_root_as_skipped_instead_of_failing(tmp_path: Path) -> None:
    present = tmp_path / "present"
    present.mkdir()
    (present / "f").write_bytes(b"x")

    stdout, summary = _run_streamer(["--root", f"a={present}", "--root", f"b={tmp_path / 'absent'}"])

    assert [info.filename for info in zipfile.ZipFile(io.BytesIO(stdout)).infolist()] == ["a/f"]
    assert summary["skipped_count"] == 1
    assert summary["skipped_paths"][0].startswith("b: root ")


def test_streamer_deflates_files_at_the_cheapest_level(tmp_path: Path) -> None:
    # The ZipFile's compresslevel never reaches an entry opened through a
    # caller-built ZipInfo, so the level must ride the entry itself: at level
    # 1 the deflate is measurably looser than zlib's best.
    generator = random.Random(0)
    payload = "".join(generator.choice("abcdefgh ") for _ in range(200_000)).encode() * 5
    root = tmp_path / "root"
    root.mkdir()
    (root / "text").write_bytes(payload)

    stdout, _summary = _run_streamer(["--root", f"a={root}"])

    entry = zipfile.ZipFile(io.BytesIO(stdout)).getinfo("a/text")
    assert entry.compress_type == zipfile.ZIP_DEFLATED
    assert len(zlib.compress(payload, 9)) < entry.compress_size < len(payload)
