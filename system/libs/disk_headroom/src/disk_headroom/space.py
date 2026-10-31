"""How much the workspace can still write to its disk.

On a cloud machine everything the workspace writes counts against a disk quota
on the machine's data filesystem, which the container cannot see: statfs here
reports only the filesystem's own free space. Either can run out first,
depending on how much of the disk the filesystem holds for its own metadata,
so the machine publishes the quota's limit and usage into the workspace's
volume (``/mngr-vol/.host-diskinfo``) and the room left is the smaller of the
two. Where no fresh file exists (a local docker workspace, or a machine that
publishes none), the filesystem's figures are the answer.

Stdlib-only, so scripts run under a bare ``python3`` can import it by putting
``src`` on ``sys.path``.
"""

import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, NamedTuple

HOST_DISKINFO_PATH: Final[Path] = Path("/mngr-vol/.host-diskinfo")
# Readers ignore a file older than this: the machine stopped publishing.
HOST_DISKINFO_MAX_AGE_SECONDS: Final[float] = 30.0
# The bytes a reader takes of the file: three short lines when well-formed.
_HOST_DISKINFO_MAX_BYTES: Final[int] = 4096


class HostQuota(NamedTuple):
    """The cloud machine's disk quota over everything the workspace writes, as last published."""

    limit: int
    used: int


class DiskSpace(NamedTuple):
    """The workspace's disk as it can use it: ``free`` is what it can still write."""

    total: int
    used: int
    free: int
    # The filesystem's own free space, the only figure statfs gives.
    filesystem_free: int
    # The quota's room left, or None where no quota applies to the path.
    quota_free: int | None

    @property
    def is_quota_bound(self) -> bool:
        """Whether the quota runs out before the filesystem does."""
        return self.quota_free is not None and self.quota_free < self.filesystem_free


def parse_host_diskinfo(text: str) -> tuple[HostQuota, float] | None:
    """The quota and the time it was published, or None for a file that is not a complete one."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        value = rest.strip()
        if value.isdecimal():
            values[key] = int(value)
    if not {"QuotaLimitBytes", "QuotaUsedBytes", "Timestamp"} <= values.keys():
        return None
    return HostQuota(
        limit=values["QuotaLimitBytes"], used=values["QuotaUsedBytes"]
    ), float(values["Timestamp"])


def read_host_quota(host_diskinfo: Path, now_seconds: float) -> HostQuota | None:
    """The published quota while the file is fresh, else None (no quota known)."""
    try:
        with host_diskinfo.open("rb") as file:
            text = file.read(_HOST_DISKINFO_MAX_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    parsed = parse_host_diskinfo(text)
    if parsed is None:
        return None
    quota, published_at = parsed
    if not 0 <= now_seconds - published_at <= HOST_DISKINFO_MAX_AGE_SECONDS:
        return None
    return quota


def is_on_same_filesystem(path: Path, other: Path) -> bool:
    """Whether ``path`` and ``other`` report the same filesystem, as statfs sees them.

    Compared by size rather than device number: under gVisor the container's
    root and its volume are separate mounts of the same filesystem, while
    ``/tmp`` is a RAM disk the quota does not cover.
    """
    try:
        path_stats, other_stats = os.statvfs(path), os.statvfs(other)
    except OSError:
        return False
    return (path_stats.f_blocks * path_stats.f_frsize) == (
        other_stats.f_blocks * other_stats.f_frsize
    )


def compute_disk_space(
    filesystem_total: int,
    filesystem_used: int,
    filesystem_free: int,
    quota: HostQuota | None,
) -> DiskSpace:
    """The figures for a path on a filesystem reading those three, whose writes count against ``quota``.

    Under a quota the total is what the workspace has used plus what it can
    still write, so the share used reads the same as how close it is to full,
    whichever limit is the nearer.
    """
    if quota is None:
        return DiskSpace(
            total=filesystem_total,
            used=filesystem_used,
            free=filesystem_free,
            filesystem_free=filesystem_free,
            quota_free=None,
        )
    quota_free = max(quota.limit - quota.used, 0)
    free = min(filesystem_free, quota_free)
    return DiskSpace(
        total=quota.used + free,
        used=quota.used,
        free=free,
        filesystem_free=filesystem_free,
        quota_free=quota_free,
    )


def read_disk_space(
    path: Path,
    host_diskinfo: Path = HOST_DISKINFO_PATH,
    clock: Callable[[], float] = time.time,
) -> DiskSpace:
    """The disk holding ``path`` as the workspace can use it. Raises OSError when ``path`` cannot be read."""
    filesystem = shutil.disk_usage(path)
    quota = (
        read_host_quota(host_diskinfo, clock())
        if is_on_same_filesystem(path, host_diskinfo.parent)
        else None
    )
    return compute_disk_space(filesystem.total, filesystem.used, filesystem.free, quota)
