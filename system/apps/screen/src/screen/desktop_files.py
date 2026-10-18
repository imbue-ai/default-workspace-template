"""Files dropped on the viewer land in the Screen's desktop folder (docs/system/specs/screen/spec.md section 4.3):
the folder ``xfdesktop`` shows as icons, which is the XDG desktop directory (``XDG_DESKTOP_DIR`` in
``~/.config/user-dirs.dirs``, the file ``xdg-user-dirs-update`` writes when the session starts) and ``~/Desktop``
without one. Each file keeps its name; a clash takes a numeric suffix, settled by an exclusive create so two drops
never write the same file."""

import os
import shutil
from pathlib import Path
from typing import Final
from typing import IO

from loguru import logger

from imbue.imbue_common.pure import pure
from screen.errors import FileDropError
from screen.primitives import DroppedFileName

# The cap on one drop, every file together (the route answers 413 past it): a few documents or pictures, never a
# disk image, since the body is held in memory or on the small tmpfs while it is parsed.
MAX_DROP_BYTES: Final[int] = 64 * 1024 * 1024

USER_DIRS_RELATIVE_PATH: Final[Path] = Path(".config/user-dirs.dirs")
DESKTOP_DIR_KEY: Final[str] = "XDG_DESKTOP_DIR"
DEFAULT_DESKTOP_DIR_NAME: Final[str] = "Desktop"
# How many suffixed names are tried for one dropped file before the drop is refused.
_MAX_CLASH_ATTEMPTS: Final[int] = 1000


@pure
def parse_desktop_dir(user_dirs_text: str, home: Path) -> Path | None:
    """The desktop folder ``user-dirs.dirs`` names, in the file's own format (``XDG_DESKTOP_DIR="$HOME/Desktop"``
    or an absolute path in quotes), or None when the file does not name one."""
    for raw_line in user_dirs_text.splitlines():
        line = raw_line.strip()
        if not line.startswith(f"{DESKTOP_DIR_KEY}="):
            continue
        quoted = line.partition("=")[2].strip()
        value = quoted[1:-1] if len(quoted) >= 2 and quoted[0] == quoted[-1] == '"' else quoted
        if value.startswith("$HOME/"):
            return home / value.removeprefix("$HOME/")
        if value.startswith("/"):
            return Path(value)
        return None
    return None


def desktop_directory(home: Path) -> Path:
    """Where ``xfdesktop`` shows icons from for this home: the user-dirs file's desktop entry, else ``~/Desktop``."""
    user_dirs_path = home / USER_DIRS_RELATIVE_PATH
    try:
        text = user_dirs_path.read_text() if user_dirs_path.is_file() else None
    except OSError as e:
        logger.debug("Could not read {}; the desktop folder is the default: {}", user_dirs_path, e)
        text = None
    parsed = parse_desktop_dir(text, home) if text is not None else None
    return parsed if parsed is not None else home / DEFAULT_DESKTOP_DIR_NAME


@pure
def suffixed_file_name(name: DroppedFileName, attempt: int) -> str:
    """The name tried on the ``attempt``-th clash: the name itself first, then ``<stem>-<attempt><suffix>``."""
    if attempt == 0:
        return str(name)
    as_path = Path(str(name))
    return f"{as_path.stem}-{attempt}{as_path.suffix}"


def save_dropped_file(directory: Path, name: DroppedFileName, stream: IO[bytes]) -> Path:
    """Write ``stream`` into ``directory`` under ``name``, or the first free suffixed name when it is taken; the folder
    is created when missing. Raises ``FileDropError`` when the folder cannot be written."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise FileDropError(f"the desktop folder {directory} could not be created: {e}") from e
    for attempt in range(_MAX_CLASH_ATTEMPTS):
        target = directory / suffixed_file_name(name, attempt)
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        except OSError as e:
            raise FileDropError(f"{target} could not be created: {e}") from e
        try:
            with os.fdopen(descriptor, "wb") as output:
                shutil.copyfileobj(stream, output)
        except OSError as e:
            # The exclusive create made the name this drop's own, so a half-written file is removed rather than
            # left on the desktop looking whole.
            target.unlink(missing_ok=True)
            raise FileDropError(f"{target} could not be written: {e}") from e
        return target
    raise FileDropError(f"{directory} already holds {_MAX_CLASH_ATTEMPTS} files named after {name}")
