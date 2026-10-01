"""`host-backup-heavy-dirs` CLI: which directories hold the entries a backup walks.

restic's backup time follows the number of files and directories it has to
walk, not their size, so a slow backup is explained by where those entries
live. This lists a snapshot (`restic ls --json`, the latest by default) and
prints the directories holding at least a given share of its entries, nested
under their parents. What it counts is what the backup actually held, with
every exclude already applied.

The listing is streamed and counted line by line: a workspace with a million
entries produces hundreds of megabytes of listing, which must not be held in
memory at once.
"""

import json
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Final

import click
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field

from host_backup.cli import EXIT_BACKUPS_NOT_CONFIGURED
from host_backup.config import (
    HOST_BACKUP_README_PATH,
    RESTIC_ENV_PATH,
    load_restic_env,
    missing_required_restic_keys,
)
from host_backup.restic import NO_BACKUP_MARKER_FILENAME, build_restic_environment

_ROOT: Final[str] = "/"

EXIT_LISTING_FAILED: Final[int] = 1


class SnapshotListingError(Exception):
    """Raised when `restic ls` exits non-zero."""


class SnapshotEntryCounts(FrozenModel):
    """How many entries a snapshot holds under each of its directories."""

    snapshot_short_id: str = Field(description="Short id of the listed snapshot")
    snapshot_time: str = Field(
        description="When the snapshot was taken, as restic reports it"
    )
    entry_count_by_directory: dict[str, int] = Field(
        description="Entries (files, directories, links) beneath each directory; '/' holds the total"
    )


class HeavyDirectory(FrozenModel):
    """A directory holding a large share of a snapshot's entries."""

    path: str = Field(description="Directory path inside the snapshot")
    entry_count: int = Field(description="Entries beneath it")
    depth: int = Field(description="Path components below the snapshot root")


@pure
def _parent_directory(path: str) -> str:
    parent = path.rsplit("/", 1)[0]
    return parent if parent else _ROOT


def count_snapshot_listing(
    listing_lines: Iterable[str], max_depth: int
) -> SnapshotEntryCounts:
    """Count a `restic ls --json` listing's entries under every directory up to `max_depth`."""
    snapshot_short_id = ""
    snapshot_time = ""
    entry_count_by_directory: dict[str, int] = {_ROOT: 0}
    for line in listing_lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            logger.warning(
                "Skipping a restic ls line that is not JSON: {!r}", line[:200]
            )
            continue
        if not isinstance(record, dict):
            continue
        message_type = record.get("message_type")
        if message_type == "snapshot":
            snapshot_short_id = str(record.get("short_id", ""))
            snapshot_time = str(record.get("time", ""))
            continue
        path = record.get("path")
        if message_type != "node" or not isinstance(path, str):
            continue

        # Credit the entry to the root and to each ancestor directory within the depth limit
        entry_count_by_directory[_ROOT] += 1
        components = path.strip("/").split("/")[:-1]
        for depth in range(1, min(len(components), max_depth) + 1):
            ancestor = "/" + "/".join(components[:depth])
            entry_count_by_directory[ancestor] = (
                entry_count_by_directory.get(ancestor, 0) + 1
            )
    return SnapshotEntryCounts(
        snapshot_short_id=snapshot_short_id,
        snapshot_time=snapshot_time,
        entry_count_by_directory=entry_count_by_directory,
    )


@pure
def select_heavy_directories(
    counts: SnapshotEntryCounts, min_share: float
) -> list[HeavyDirectory]:
    """The directories holding at least `min_share` of all entries, each followed by its heavy children."""
    total = counts.entry_count_by_directory[_ROOT]
    if total == 0:
        return []
    min_entry_count = total * min_share

    # Index each directory's subdirectories, heaviest first
    children_by_parent: dict[str, list[str]] = {}
    for path in counts.entry_count_by_directory:
        if path != _ROOT:
            children_by_parent.setdefault(_parent_directory(path), []).append(path)
    for children in children_by_parent.values():
        children.sort(key=lambda child: -counts.entry_count_by_directory[child])

    # Walk down from the root, descending only into directories over the share
    heavy: list[HeavyDirectory] = []
    pending: list[tuple[str, int]] = [
        (child, 1) for child in reversed(children_by_parent.get(_ROOT, []))
    ]
    while pending:
        path, depth = pending.pop()
        entry_count = counts.entry_count_by_directory[path]
        if entry_count < min_entry_count:
            continue
        heavy.append(HeavyDirectory(path=path, entry_count=entry_count, depth=depth))
        pending.extend(
            (child, depth + 1) for child in reversed(children_by_parent.get(path, []))
        )
    return heavy


@pure
def format_heavy_directories_report(
    counts: SnapshotEntryCounts, heavy: Sequence[HeavyDirectory], min_share: float
) -> str:
    total = counts.entry_count_by_directory[_ROOT]
    lines = [
        f"Snapshot {counts.snapshot_short_id} ({counts.snapshot_time}) holds "
        f"{total:,} entries (files, directories and links).",
        f"Directories holding at least {min_share * 100:g}% of them, heaviest first under each parent:",
        "",
    ]
    for directory in heavy:
        indent = "  " * (directory.depth - 1)
        lines.append(
            f"{directory.entry_count:>12,}  {directory.entry_count / total:6.1%}  {indent}{directory.path}"
        )
    lines += [
        "",
        "Paths are as the snapshot recorded them: an hourly backup's are relative to the backed-up",
        "home directory (/home/user in a workspace).",
        f"Keep a directory that can be rebuilt out of the backup with a `{NO_BACKUP_MARKER_FILENAME}` file",
        "in it, or a pattern in `extra_excludes` in data/system/backup.toml.",
        f"See 'Slow backups' in {HOST_BACKUP_README_PATH}.",
    ]
    return "\n".join(lines)


def stream_snapshot_listing(
    snapshot: str, env_overrides: Mapping[str, str]
) -> Iterator[str]:
    """Yield `restic ls --json <snapshot>` output line by line; raise SnapshotListingError if restic fails.

    `--no-lock` lets the listing run beside a backup or prune, which it only reads around.
    """
    with tempfile.TemporaryFile(mode="w+") as stderr_file:
        with subprocess.Popen(
            ["restic", "--no-lock", "ls", "--json", snapshot],
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            env=build_restic_environment(env_overrides),
        ) as process:
            assert process.stdout is not None
            yield from process.stdout
        if process.returncode != 0:
            stderr_file.seek(0)
            raise SnapshotListingError(
                f"restic ls exited {process.returncode}: {stderr_file.read().strip()}"
            )


@click.command()
@click.option(
    "--snapshot",
    default="latest",
    show_default=True,
    help="Snapshot to list (an id, or `latest`)",
)
@click.option(
    "--min-share",
    default=0.02,
    show_default=True,
    type=click.FloatRange(min=0.0, max=1.0),
    help="Show directories holding at least this fraction of all entries",
)
@click.option(
    "--max-depth",
    default=8,
    show_default=True,
    type=click.IntRange(min=1),
    help="Deepest directory level to count under",
)
def heavy_dirs_main(snapshot: str, min_share: float, max_depth: int) -> None:
    """Show which directories hold the files and directories a backup has to walk."""
    env = load_restic_env()
    missing = missing_required_restic_keys(env)
    if missing:
        logger.error(
            "Backups are not configured: {} is missing {} (run this from the workspace root)",
            RESTIC_ENV_PATH.absolute(),
            ", ".join(missing),
        )
        sys.exit(EXIT_BACKUPS_NOT_CONFIGURED)
    try:
        counts = count_snapshot_listing(
            stream_snapshot_listing(snapshot, env), max_depth=max_depth
        )
    except SnapshotListingError as e:
        logger.error("Could not list snapshot {}: {}", snapshot, e)
        sys.exit(EXIT_LISTING_FAILED)
    heavy = select_heavy_directories(counts, min_share=min_share)
    click.echo(format_heavy_directories_report(counts, heavy, min_share=min_share))


if __name__ == "__main__":
    heavy_dirs_main()
