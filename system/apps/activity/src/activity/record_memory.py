"""The once-a-minute recorder behind the memory-over-time chart: one reading, appended, and out.

cron runs it (``cron_entry``), so it costs nothing between readings and nothing while System Monitor is closed. It
reads memory exactly as the page does (``memory_reading``), so the chart and the headline agree.
"""

import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

import click
from loguru import logger

from activity.history import HISTORY_PATH
from activity.history import MemorySample
from activity.history import RETENTION_SECONDS
from activity.history import append_sample
from activity.history import epoch_seconds
from activity.memory_reading import BYTES_PER_KIB
from activity.memory_reading import DEFAULT_MEMORY_SOURCES
from activity.memory_reading import MemorySources
from activity.memory_reading import read_memory

EXIT_NO_READING: Final[int] = 1


def record_once(sources: MemorySources, history_path: Path, now: datetime) -> bool:
    """Append one reading; False when no memory source answered."""
    reading = read_memory(sources)
    if reading is None:
        return False
    append_sample(
        history_path,
        MemorySample(
            at_epoch_seconds=epoch_seconds(now),
            used_kib=reading.used_bytes // BYTES_PER_KIB,
            limit_kib=reading.limit_bytes // BYTES_PER_KIB,
        ),
        retention_seconds=RETENTION_SECONDS,
    )
    return True


@click.command()
@click.option(
    "--history",
    "history_path",
    type=click.Path(path_type=Path),
    default=HISTORY_PATH,
    show_default=True,
    help="The file the readings are appended to",
)
def main(history_path: Path) -> None:
    """Record the workspace's memory use once, for System Monitor's memory-over-time chart."""
    if not record_once(DEFAULT_MEMORY_SOURCES, history_path, datetime.now(timezone.utc)):
        logger.warning("No memory reading was available; nothing recorded")
        sys.exit(EXIT_NO_READING)


if __name__ == "__main__":
    main()
