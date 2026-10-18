"""Where a dropped file lands and under what name: the desktop folder xfdesktop shows, the name checks, and the
suffix a clash takes."""

import io
from pathlib import Path

import pytest

from screen.desktop_files import desktop_directory
from screen.desktop_files import parse_desktop_dir
from screen.desktop_files import save_dropped_file
from screen.desktop_files import suffixed_file_name
from screen.errors import FileDropError
from screen.errors import InvalidDroppedFileNameError
from screen.primitives import DroppedFileName
from screen.primitives import MAX_DROPPED_FILE_NAME_BYTES
from screen.primitives import describe_dropped_file_name_problem

_USER_DIRS = """# This file is written by xdg-user-dirs-update
XDG_DESKTOP_DIR="$HOME/Desktop"
XDG_DOWNLOAD_DIR="$HOME/Downloads"
"""


def test_the_user_dirs_file_names_the_desktop_folder_relative_to_home_or_absolutely() -> None:
    home = Path("/home/someone")
    assert parse_desktop_dir(_USER_DIRS, home) == home / "Desktop"
    assert parse_desktop_dir('XDG_DESKTOP_DIR="$HOME/Schreibtisch"\n', home) == home / "Schreibtisch"
    assert parse_desktop_dir('XDG_DESKTOP_DIR="/srv/desk"\n', home) == Path("/srv/desk")
    assert parse_desktop_dir('XDG_DOWNLOAD_DIR="$HOME/Downloads"\n', home) is None
    assert parse_desktop_dir("", home) is None
    assert parse_desktop_dir('XDG_DESKTOP_DIR="relative/desk"\n', home) is None


def test_the_desktop_folder_falls_back_to_home_desktop_without_a_user_dirs_file(tmp_path: Path) -> None:
    assert desktop_directory(tmp_path) == tmp_path / "Desktop"
    user_dirs = tmp_path / ".config" / "user-dirs.dirs"
    user_dirs.parent.mkdir(parents=True)
    user_dirs.write_text('XDG_DESKTOP_DIR="$HOME/Bureau"\n')
    assert desktop_directory(tmp_path) == tmp_path / "Bureau"


@pytest.mark.parametrize(
    "name",
    ["", "   ", "../escape.txt", "dir/file.txt", "dir\\file.txt", ".hidden", "..", "bad\x00name", "tab\tname"],
)
def test_a_name_that_is_not_one_plain_segment_is_refused(name: str) -> None:
    assert describe_dropped_file_name_problem(name) is not None
    with pytest.raises(InvalidDroppedFileNameError):
        DroppedFileName(name)


def test_a_name_over_the_filesystem_limit_is_refused_and_a_plain_one_kept() -> None:
    assert describe_dropped_file_name_problem("x" * MAX_DROPPED_FILE_NAME_BYTES) is None
    assert describe_dropped_file_name_problem("x" * (MAX_DROPPED_FILE_NAME_BYTES + 1)) is not None
    assert DroppedFileName("Quarterly report (final).pdf") == "Quarterly report (final).pdf"
    assert DroppedFileName("notes") == "notes"


def test_a_clash_takes_a_numeric_suffix_before_the_extension() -> None:
    assert suffixed_file_name(DroppedFileName("report.pdf"), 0) == "report.pdf"
    assert suffixed_file_name(DroppedFileName("report.pdf"), 1) == "report-1.pdf"
    assert suffixed_file_name(DroppedFileName("report.pdf"), 12) == "report-12.pdf"
    assert suffixed_file_name(DroppedFileName("notes"), 1) == "notes-1"


def test_saving_creates_the_folder_keeps_the_name_and_suffixes_a_clash(tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop"
    first = save_dropped_file(desktop, DroppedFileName("report.pdf"), io.BytesIO(b"one"))
    second = save_dropped_file(desktop, DroppedFileName("report.pdf"), io.BytesIO(b"two"))
    third = save_dropped_file(desktop, DroppedFileName("report.pdf"), io.BytesIO(b"three"))
    assert [first.name, second.name, third.name] == ["report.pdf", "report-1.pdf", "report-2.pdf"]
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"
    assert third.read_bytes() == b"three"
    assert (first.stat().st_mode & 0o777) == 0o644


def test_a_folder_that_cannot_be_created_is_a_file_drop_error(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    with pytest.raises(FileDropError):
        save_dropped_file(blocker / "Desktop", DroppedFileName("report.pdf"), io.BytesIO(b"one"))
