from pathlib import Path

from inline_snapshot import snapshot

from screen.user_dirs import USER_DIRS_CONF_RELATIVE_PATH
from screen.user_dirs import USER_DIRS_RELATIVE_PATH
from screen.user_dirs import render_user_dirs
from screen.user_dirs import write_user_dirs


def test_a_fresh_file_names_the_folder_for_the_desktop_and_downloads() -> None:
    rendered = render_user_dirs(
        None, Path("/home/user/workspace/data/Desktop"), Path("/home/user/workspace/data/Downloads")
    )
    assert rendered == snapshot("""\
# Rendered by the Screen service before each stack start; the two folders below are its.
XDG_DESKTOP_DIR="/home/user/workspace/data/Desktop"
XDG_DOWNLOAD_DIR="/home/user/workspace/data/Downloads"
""")


def test_an_existing_file_keeps_its_other_entries_and_is_stable_when_rendered_again() -> None:
    existing = """# This file is written by xdg-user-dirs-update
XDG_DESKTOP_DIR="$HOME/Desktop"
XDG_DOWNLOAD_DIR="$HOME/Downloads"
XDG_DOCUMENTS_DIR="$HOME/Documents"
"""
    rendered = render_user_dirs(existing, Path("/srv/ws/data/Desktop"), Path("/srv/ws/data/Downloads"))
    assert rendered == snapshot("""\
# Rendered by the Screen service before each stack start; the two folders below are its.
# This file is written by xdg-user-dirs-update
XDG_DOCUMENTS_DIR="$HOME/Documents"
XDG_DESKTOP_DIR="/srv/ws/data/Desktop"
XDG_DOWNLOAD_DIR="/srv/ws/data/Downloads"
""")
    assert render_user_dirs(rendered, Path("/srv/ws/data/Desktop"), Path("/srv/ws/data/Downloads")) == rendered


def test_the_files_are_written_into_the_home_and_the_folder_created(tmp_path: Path) -> None:
    home = tmp_path / "home"
    desktop_dir = tmp_path / "workspace" / "data" / "Desktop"
    download_dir = tmp_path / "workspace" / "data" / "Downloads"
    write_user_dirs(home, desktop_dir, download_dir)
    assert desktop_dir.is_dir() and download_dir.is_dir()
    assert f'XDG_DOWNLOAD_DIR="{download_dir}"' in (home / USER_DIRS_RELATIVE_PATH).read_text()
    assert (home / USER_DIRS_CONF_RELATIVE_PATH).read_text() == "enabled=False\n"
    written_at = (home / USER_DIRS_RELATIVE_PATH).stat().st_mtime_ns
    write_user_dirs(home, desktop_dir, download_dir)
    assert (home / USER_DIRS_RELATIVE_PATH).stat().st_mtime_ns == written_at
