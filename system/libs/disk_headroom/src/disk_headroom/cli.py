"""``disk-headroom [PATH] [--json]``: how much the workspace can still write to the disk holding PATH.

Agents run it (``uv run --no-sync disk-headroom``) where they would run ``df``,
which on a cloud machine reports the filesystem's free space and never the
disk quota that may run out first.
"""

import json
from pathlib import Path
from typing import Final

import click

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
        detail = "limited by: the disk (no disk quota is published for it)"
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
    return f"{headline}\n{detail}"


@click.command(
    help="How much the workspace can still write to the disk holding PATH (default: /)."
)
@click.argument("path", required=False, default="/", type=click.Path(path_type=Path))
@click.option(
    "--json", "is_json", is_flag=True, help="Print the figures, in bytes, as JSON."
)
def main(path: Path, is_json: bool) -> None:
    try:
        space = read_disk_space(path)
    except OSError as exc:
        raise click.ClickException(f"cannot read {path}: {exc}") from exc
    if is_json:
        click.echo(
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
        )
    else:
        click.echo(format_disk_space(space))
