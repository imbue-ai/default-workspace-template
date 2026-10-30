"""``disk-headroom [PATH] [--json]``: how much the workspace can still write to the disk holding PATH.

Agents run it (``uv run --no-sync disk-headroom``) where they would run ``df``,
which on a cloud machine reports the filesystem's free space and never the
disk quota that may run out first.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from disk_headroom.space import DiskSpace, read_disk_space

_GIB: Final[int] = 1024**3


def _gib(count: int) -> str:
    return f"{count / _GIB:.1f} GiB"


def format_disk_space(space: DiskSpace) -> str:
    """Two lines: what can still be written, and which limit it comes from."""
    headline = (
        f"free: {_gib(space.free)} of {_gib(space.total)} ({_gib(space.used)} used)"
    )
    if space.quota_free is None:
        detail = "limited by: the disk (no disk quota applies here)"
    elif space.is_quota_bound:
        detail = (
            f"limited by: the machine's disk quota ({_gib(space.quota_free)} left);"
            f" the disk itself reports {_gib(space.filesystem_free)} free"
        )
    else:
        detail = (
            f"limited by: the disk ({_gib(space.filesystem_free)} free);"
            f" the machine's disk quota has {_gib(space.quota_free)} left"
        )
    return f"{headline}\n{detail}\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="disk-headroom",
        description="How much the workspace can still write to the disk holding PATH.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="/",
        type=Path,
        help="A path on the disk to measure (default: /).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the figures, in bytes, as JSON."
    )
    args = parser.parse_args(argv)
    try:
        space = read_disk_space(args.path)
    except OSError as exc:
        sys.stderr.write(f"disk-headroom: cannot read {args.path}: {exc}\n")
        return 1
    if args.json:
        sys.stdout.write(
            json.dumps(
                {
                    "total": space.total,
                    "used": space.used,
                    "free": space.free,
                    "filesystem_free": space.filesystem_free,
                    "quota_free": space.quota_free,
                    "is_quota_bound": space.is_quota_bound,
                }
            )
            + "\n"
        )
    else:
        sys.stdout.write(format_disk_space(space))
    return 0


if __name__ == "__main__":
    sys.exit(main())
