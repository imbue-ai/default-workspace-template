"""The user's timezone as the workspace records it: one IANA name in ``data/.state/user_timezone``.

The client that creates a workspace writes its user's zone once, and the bootstrap applies whatever
the file names when the container boots (``bootstrap.manager``), so a write takes effect at the next
boot. The file belongs to the user's first client: a later write replaces a recorded zone only when
it says so.
"""

import os
import tempfile
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

from workspace_profile.errors import InvalidTimezoneError, ProfileWriteError

# Relative to the workspace root every supervised process runs from; the bootstrap reads the same file.
USER_TIMEZONE_RELATIVE_PATH: Final[Path] = Path("data/.state/user_timezone")


def read_user_timezone(workspace_root: Path) -> str:
    """The zone the workspace records, or '' when it records none (or the file cannot be read)."""
    path = workspace_root / USER_TIMEZONE_RELATIVE_PATH
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Could not read the user timezone at {}: {}", path, e)
        return ""


def _require_zone(zone: str) -> str:
    name = zone.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as e:
        raise InvalidTimezoneError(f"{zone!r} is not a timezone") from e
    return name


def write_user_timezone(workspace_root: Path, zone: str, is_replacing: bool) -> bool:
    """Record ``zone``; whether it was written.

    With ``is_replacing`` false, a zone already on record is kept and nothing is written. Raises
    ``InvalidTimezoneError`` for a name that is not an IANA zone, and ``ProfileWriteError`` when the
    file cannot be written.
    """
    name = _require_zone(zone)
    if not is_replacing and read_user_timezone(workspace_root):
        return False
    path = workspace_root / USER_TIMEZONE_RELATIVE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}."
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(name + "\n")
            os.replace(temp_name, path)
        except OSError:
            os.unlink(temp_name)
            raise
    except OSError as e:
        raise ProfileWriteError(f"Could not write {path}: {e}") from e
    return True
