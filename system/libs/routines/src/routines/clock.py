import os
import threading
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from datetime import tzinfo
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from loguru import logger

from routines.interfaces import ClockInterface

TIMEZONE_ENV: Final[str] = "TZ"
# What the bootstrap points at the user's zone: the name in one, the zoneinfo file in the other.
TIMEZONE_NAME_PATH: Final[Path] = Path("/etc/timezone")
LOCALTIME_PATH: Final[Path] = Path("/etc/localtime")
ZONEINFO_DIR: Final[Path] = Path("/usr/share/zoneinfo")


def _zone_name_of_localtime(localtime_path: Path, zoneinfo_dir: Path) -> str:
    """The zone a localtime symlink names, by where it points under the zoneinfo directory; empty otherwise."""
    try:
        target = localtime_path.resolve(strict=True)
        return str(target.relative_to(zoneinfo_dir.resolve()))
    except (OSError, ValueError) as e:
        logger.debug("The localtime link {} names no zone under {}: {}", localtime_path, zoneinfo_dir, e)
        return ""


def resolve_local_zone(
    environ: Mapping[str, str],
    timezone_name_path: Path,
    localtime_path: Path,
    zoneinfo_dir: Path,
) -> tzinfo:
    """The machine's zone with its daylight-saving rules: TZ, else the timezone name file, else what the
    localtime link points at; the current fixed offset when none of them names a zone the zoneinfo database
    knows."""
    candidates = [environ.get(TIMEZONE_ENV, "")]
    try:
        candidates.append(timezone_name_path.read_text().strip())
    except OSError as e:
        logger.debug("The timezone name file {} could not be read: {}", timezone_name_path, e)
    candidates.append(_zone_name_of_localtime(localtime_path, zoneinfo_dir))
    for name in candidates:
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("The local zone {!r} is not in the zoneinfo database; trying the next source", name)
    zone = datetime.now().astimezone().tzinfo
    assert zone is not None
    return zone


class SystemClock(ClockInterface):
    """The machine's clock and zone; a pause is an uncontested wait on an event nobody sets."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def local_zone(self) -> tzinfo:
        return resolve_local_zone(os.environ, TIMEZONE_NAME_PATH, LOCALTIME_PATH, ZONEINFO_DIR)

    def pause(self, seconds: float) -> None:
        threading.Event().wait(seconds)
