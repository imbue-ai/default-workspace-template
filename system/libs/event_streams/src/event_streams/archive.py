"""The archive rule for an append-only events.jsonl stream: stage a stream past its threshold aside, then gzip it.

A program that appends to one stream forever would fill the disk, and a consumer that replays the stream from its
start (mngr's event replay, the analytics collector) would read ever more. So a writer checks the stream's size
before a write and moves an oversized one aside under a timestamped name (a rename, so it is quick enough to run
on the write path), and compresses the staged files on a thread of its own afterwards, keeping only the newest
archives. The archive name is not one mngr's rotated-file replay reads (``events.jsonl.<digits>``), so a replay
sees the current stream alone.
"""

import gzip
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

STAGED_STREAM_SUFFIX: Final[str] = ".archiving"
STREAM_ARCHIVE_SUFFIX: Final[str] = ".gz"


def rotation_timestamp() -> str:
    """A UTC timestamp for a rotated file's name (YYYYMMDDHHMMSSffffff), unique to the microsecond."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond:06d}"


def staged_streams(events_path: Path) -> list[Path]:
    """The streams staged beside ``events_path`` and not yet compressed, oldest first."""
    return sorted(
        events_path.parent.glob(f"{events_path.name}.*{STAGED_STREAM_SUFFIX}")
    )


def stage_oversized_stream(events_path: Path, threshold_bytes: int) -> bool:
    """Move a stream over the threshold aside, so the next write starts a new one; answers whether it did.

    Only renames, so it is quick enough to run before a write; ``compress_staged_streams`` compresses.
    """
    try:
        stream_size = events_path.stat().st_size
    except FileNotFoundError:
        return False
    if stream_size <= threshold_bytes:
        return False
    events_path.rename(
        events_path.with_name(
            f"{events_path.name}.{rotation_timestamp()}{STAGED_STREAM_SUFFIX}"
        )
    )
    return True


def compress_staged_streams(events_path: Path, kept_archive_count: int) -> None:
    """Gzip every stream staged beside ``events_path`` into its archive, then keep only the newest archives."""
    for staged_path in staged_streams(events_path):
        archive_path = staged_path.with_name(
            staged_path.name.removesuffix(STAGED_STREAM_SUFFIX) + STREAM_ARCHIVE_SUFFIX
        )
        partial_path = archive_path.with_name(archive_path.name + ".partial")
        # The fastest level keeps the one-off CPU burst short in a container shared with the user's agents.
        with (
            open(staged_path, "rb") as source,
            gzip.open(partial_path, "wb", compresslevel=1) as target,
        ):
            shutil.copyfileobj(source, target)
        partial_path.rename(archive_path)
        staged_path.unlink()

    archive_paths = sorted(
        events_path.parent.glob(f"{events_path.name}.*{STREAM_ARCHIVE_SUFFIX}")
    )
    for expired_path in archive_paths[:-kept_archive_count]:
        expired_path.unlink()
