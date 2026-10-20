"""The Screen's desktop and download folders are two folders under the workspace's ``data/`` (docs/system/specs/
screen/spec.md section 4.3), where the Files app shows them: the service renders ``~/.config/user-dirs.dirs`` before
each stack start so ``XDG_DESKTOP_DIR`` and ``XDG_DOWNLOAD_DIR`` name them, keeping the file's other entries, and
turns ``xdg-user-dirs-update`` off in ``~/.config/user-dirs.conf`` so the session never moves them back. Chromium
takes its download folder from ``XDG_DOWNLOAD_DIR`` at each start, and resets one that equals the desktop folder to
its own default, which is why the two are not one."""

from pathlib import Path
from typing import Final

from imbue.imbue_common.pure import pure
from screen.xfce_helpers import write_if_changed

USER_DIRS_RELATIVE_PATH: Final[Path] = Path(".config/user-dirs.dirs")
USER_DIRS_CONF_RELATIVE_PATH: Final[Path] = Path(".config/user-dirs.conf")
DESKTOP_DIR_KEY: Final[str] = "XDG_DESKTOP_DIR"
DOWNLOAD_DIR_KEY: Final[str] = "XDG_DOWNLOAD_DIR"
_RENDERED_KEYS: Final[tuple[str, ...]] = (DESKTOP_DIR_KEY, DOWNLOAD_DIR_KEY)
_USER_DIRS_HEADER: Final[str] = (
    "# Rendered by the Screen service before each stack start; the two folders below are its.\n"
)
USER_DIRS_CONF_TEXT: Final[str] = "enabled=False\n"


@pure
def render_user_dirs(existing: str | None, desktop_dir: Path, download_dir: Path) -> str:
    """``user-dirs.dirs`` with the desktop and download entries naming the two folders and every other line kept."""
    kept = [
        line
        for line in (existing or "").splitlines()
        if not any(line.strip().startswith(f"{key}=") for key in _RENDERED_KEYS)
        and line.strip() != _USER_DIRS_HEADER.strip()
    ]
    rendered = [_USER_DIRS_HEADER.rstrip("\n"), *kept] if kept else [_USER_DIRS_HEADER.rstrip("\n")]
    rendered += [f'{DESKTOP_DIR_KEY}="{desktop_dir}"', f'{DOWNLOAD_DIR_KEY}="{download_dir}"']
    return "\n".join(rendered) + "\n"


def write_user_dirs(home: Path, desktop_dir: Path, download_dir: Path) -> None:
    """Render the two files into ``home`` and create the folders, so the session and Chromium find them at their
    start."""
    desktop_dir.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)
    write_if_changed(
        home / USER_DIRS_RELATIVE_PATH, lambda existing: render_user_dirs(existing, desktop_dir, download_dir)
    )
    write_if_changed(home / USER_DIRS_CONF_RELATIVE_PATH, lambda _existing: USER_DIRS_CONF_TEXT)
