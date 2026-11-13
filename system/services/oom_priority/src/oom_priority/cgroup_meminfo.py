"""The container's own memory headroom, read from its cgroup, in the form
earlyoom's ``--host-meminfo`` takes.

Under runc the container's ``/proc/meminfo`` is the host kernel's, so an
earlyoom reading it alone would act at 10% of the whole machine while the
container's cgroup OOM killer acts at the workspace's cap. The cgroup v2 memory
controller the container runs in is what it sees at ``/sys/fs/cgroup`` (a
cgroup namespace), and from it the container can compute for itself the file a
gen-2 slice VM publishes from outside under gVisor: the limit as ``MemTotal``
and, as ``MemAvailable``, the limit minus the usage plus what the kernel frees
before it kills anything in the cgroup: the file cache on both LRU lists that
no process has mapped, and the reclaimable slab (dentries and inodes).

Everything the container reads or walks is charged to this cgroup and stays
cached until the cgroup reaches its limit, where the kernel drops it on demand.
A page read twice moves to the active list, so a workspace whose hourly backup
walks a large tree holds most of its cap on the active list and in the inode
cache while its processes use a fraction of it: counting the inactive list
alone would read that as a full cgroup. Mapped file pages are left out: they
are the running programs' own code, and freeing them is what thrashes.

This is deliberately not the gen-2 publisher's formula
(``imbue_common.container_meminfo_publisher`` in mngr), which adds the inactive
file cache alone. That one measures a gVisor sandbox, whose own page cache
absorbs repeat reads, so the host kernel sees each page once and the cache it
charges stays on the inactive list.

The file is touched only on a definite answer. A limit that reads ``max`` (an
uncapped container) removes it, so earlyoom falls back to ``/proc/meminfo``,
which is the truth there. A cgroup with no v2 memory controller (a lima VM's
root cgroup, a cgroup v1 host, gVisor's own cgroupfs) and a transient read
error both leave whatever is there alone: on a gen-2 slice that is the VM's
file, and a stale file costs nothing, since earlyoom ignores one older than
5 s. Under gVisor the publisher has nothing to add at all (``uname -r`` names
it), so it reports that and the program exits.

Stdlib-only (see ``paths``): run by a supervisord program under a plain
``python3``.
"""

import enum
import os
from pathlib import Path
from typing import Final

# Where earlyoom.conf points --host-meminfo. On a gen-2 slice this directory is
# the host volume mount and the VM writes the file; on local docker it is a
# plain directory this publisher creates.
HOST_MEMINFO_PATH: Final[Path] = Path("/mngr-vol/.host-meminfo")
# The container's own cgroup v2 memory controller, as the cgroup namespace shows it.
CGROUP_DIR: Final[Path] = Path("/sys/fs/cgroup")
# Well inside the 5 s staleness earlyoom tolerates, and what the gen-2 VM uses.
PUBLISH_INTERVAL_SECONDS: Final[float] = 0.25

# The memory.stat fields the reclaimable figure is built from.
_INACTIVE_FILE_KEY: Final[str] = "inactive_file"
_ACTIVE_FILE_KEY: Final[str] = "active_file"
_MAPPED_FILE_KEY: Final[str] = "file_mapped"
_RECLAIMABLE_SLAB_KEY: Final[str] = "slab_reclaimable"
_UNCAPPED_LIMIT: Final[str] = "max"


class PublishOutcome(enum.Enum):
    """What one publishing round did; the publisher logs each change of outcome."""

    PUBLISHED = "published"
    UNCAPPED = "uncapped"
    NO_CONTROLLER = "no_controller"
    UNREADABLE = "unreadable"
    GVISOR = "gvisor"


def is_gvisor_kernel(kernel_release: str) -> bool:
    """Whether ``uname -r`` names gVisor (it reports ``4.19.0-gvisor``)."""
    return "gvisor" in kernel_release.lower()


def _read_first_line(path: Path) -> str | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    return text.partition("\n")[0].strip()


def _read_reclaimable_bytes(stat_path: Path) -> int | None:
    """What the kernel frees from the cgroup before it kills in it, or None when ``memory.stat`` cannot be read.

    ``file_mapped`` also counts mapped shmem, which is on neither file list, so
    taking all of it off the active list can only understate the figure.
    """
    try:
        lines = stat_path.read_text().splitlines()
    except OSError:
        return None
    stat_by_key: dict[str, int] = {}
    for line in lines:
        key, _, value = line.partition(" ")
        if value.strip().isdigit():
            stat_by_key[key] = int(value)
    unmapped_active_file = max(
        0, stat_by_key.get(_ACTIVE_FILE_KEY, 0) - stat_by_key.get(_MAPPED_FILE_KEY, 0)
    )
    return (
        stat_by_key.get(_INACTIVE_FILE_KEY, 0)
        + unmapped_active_file
        + stat_by_key.get(_RECLAIMABLE_SLAB_KEY, 0)
    )


def read_cgroup_memory_headroom(cgroup_dir: Path) -> tuple[int, int] | PublishOutcome:
    """``(limit_bytes, available_bytes)`` for a capped cgroup, else the outcome that stops the round.

    Available is the limit minus the usage plus the reclaimable part of it
    (unmapped file cache and reclaimable slab), clamped
    to ``[0, limit]``. The outcomes: ``PublishOutcome.NO_CONTROLLER`` when there is no
    ``memory.max`` to read, ``PublishOutcome.UNCAPPED`` when it reads ``max``, and
    ``PublishOutcome.UNREADABLE`` when ``memory.max`` or ``memory.current`` is not a
    number or ``memory.current`` or ``memory.stat`` cannot be read.
    """
    limit_text = _read_first_line(cgroup_dir / "memory.max")
    if limit_text is None:
        return PublishOutcome.NO_CONTROLLER
    if limit_text == _UNCAPPED_LIMIT:
        return PublishOutcome.UNCAPPED
    usage_text = _read_first_line(cgroup_dir / "memory.current")
    if not limit_text.isdigit() or usage_text is None or not usage_text.isdigit():
        return PublishOutcome.UNREADABLE
    reclaimable = _read_reclaimable_bytes(cgroup_dir / "memory.stat")
    if reclaimable is None:
        return PublishOutcome.UNREADABLE
    limit = int(limit_text)
    available = max(0, min(limit, limit - int(usage_text) + reclaimable))
    return limit, available


def render_host_meminfo(
    limit_bytes: int, available_bytes: int, timestamp_seconds: int
) -> str:
    """The file in the ``/proc/meminfo`` subset earlyoom's ``--host-meminfo`` reads."""
    return (
        f"MemTotal: {limit_bytes // 1024} kB\n"
        f"MemAvailable: {available_bytes // 1024} kB\n"
        f"Timestamp: {timestamp_seconds}\n"
    )


def write_replacing(target_path: Path, content: str) -> None:
    """Replace the file by rename, so a reader (earlyoom on every poll, the system monitor) never sees a half-written one."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f"{target_path.name}.{os.getpid()}.tmp")
    temp_path.write_text(content)
    os.replace(temp_path, target_path)


def publish_once(
    cgroup_dir: Path, target_path: Path, kernel_release: str, now_seconds: int
) -> PublishOutcome:
    """One publishing round: write the headroom, remove the file of an uncapped container, or leave it be.

    Returns the ``PublishOutcome`` naming what happened.
    """
    if is_gvisor_kernel(kernel_release):
        return PublishOutcome.GVISOR
    headroom = read_cgroup_memory_headroom(cgroup_dir)
    if headroom is PublishOutcome.UNCAPPED:
        target_path.unlink(missing_ok=True)
        return PublishOutcome.UNCAPPED
    if isinstance(headroom, PublishOutcome):
        return headroom
    limit_bytes, available_bytes = headroom
    write_replacing(
        target_path, render_host_meminfo(limit_bytes, available_bytes, now_seconds)
    )
    return PublishOutcome.PUBLISHED
