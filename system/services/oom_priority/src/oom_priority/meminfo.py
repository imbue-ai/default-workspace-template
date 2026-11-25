"""Memory in use, read where the memory guard reads it.

Under docker the workspace publishes its cgroup's limit and headroom to the
headroom file, and under gVisor the gen-2 slice VM does; there ``/proc/meminfo``
describes the whole host or misses what the runtime is charged. earlyoom reads
the file while it is fresh (``--host-meminfo``, ``earlyoom.conf``), and so does
this, falling back to ``/proc/meminfo``. The resource sampler records memory
through it, and the system monitor app shows the same figures.

It also reads one process's anonymous memory, which the memory candidates command
(``bin/memory_candidates.py``) sums per candidate.

Stdlib-only (see ``paths``): the sampler runs under a plain ``python3``.
"""

import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, NamedTuple

from oom_priority.cgroup_meminfo import HOST_MEMINFO_PATH

PROC: Final[Path] = Path("/proc")
KIB: Final[int] = 1024
# earlyoom ignores the headroom file once it is older than this.
HOST_MEMINFO_MAX_AGE_SECONDS: Final[float] = 5.0


def parse_kib_fields(text: str) -> dict[str, int]:
    """``Key: <n> kB`` lines (/proc/meminfo, smaps_rollup) as bytes by key."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0]) * KIB
    return values


def is_host_meminfo_fresh(host_meminfo: Path, now_seconds: float) -> bool:
    """Whether the headroom file is new enough that earlyoom still acts on it.

    One rule for every reader of the guard's figure (``read_meminfo`` here, the
    candidates command's own ``read_free_memory``), since it is earlyoom's rule
    and not ours. A file that cannot be stat'd is stale: there is nothing to act
    on, so ``/proc/meminfo`` is the answer.
    """
    try:
        return (
            now_seconds - host_meminfo.stat().st_mtime <= HOST_MEMINFO_MAX_AGE_SECONDS
        )
    except OSError:
        return False


def read_meminfo(
    proc: Path = PROC,
    host_meminfo: Path = HOST_MEMINFO_PATH,
    clock: Callable[[], float] = time.time,
) -> dict[str, int]:
    """Total, available, and used memory in bytes: the figures the memory guard acts on."""
    values: dict[str, int] = {}
    if is_host_meminfo_fresh(host_meminfo, clock()):
        try:
            values = parse_kib_fields(host_meminfo.read_text())
        except OSError:
            values = {}
    if "MemTotal" not in values or "MemAvailable" not in values:
        values = parse_kib_fields((proc / "meminfo").read_text())
    total = values["MemTotal"]
    available = values.get("MemAvailable", values.get("MemFree", 0))
    return {"total": total, "available": available, "used": total - available}


class SmapsMemory(NamedTuple):
    """What one read of a process's ``/proc/<pid>/smaps`` says about the memory it holds, in KiB."""

    anonymous_kib: int
    # The resident size of each shared memory segment the process maps (a memfd, a /dev/shm file,
    # a System V segment), keyed by the mapping's "<device> <inode>", so a reader summing several
    # processes can count each segment once.
    shared_segment_kib: Mapping[str, int]


def read_smaps_memory(pid: int, proc: Path = PROC) -> SmapsMemory | None:
    """A process's anonymous memory and shared memory segments from ``/proc/<pid>/smaps``, or None
    when it is gone or unreadable.

    This is the reading under gVisor, which has no ``smaps_rollup`` and reports every page as
    private (``Pss`` equals ``Rss`` there), so ``Anonymous`` is the one figure that tells the
    memory a process made for itself from the files it maps.
    """
    try:
        text = (proc / str(pid) / "smaps").read_text(errors="replace")
    except OSError:
        return None
    segments: dict[str, int] = {}
    for mapping in _SHARED_MAPPING.finditer(text):
        rss = _RSS_LINE.search(mapping.group("fields"))
        key = f"{mapping.group('device')} {mapping.group('inode')}"
        segments[key] = max(segments.get(key, 0), int(rss.group(1)) if rss else 0)
    return SmapsMemory(
        anonymous_kib=_summed_anonymous_kib(text), shared_segment_kib=segments
    )


def read_process_anonymous_kib(pid: int, proc: Path = PROC) -> int | None:
    """A process's anonymous memory in KiB: the ``Anonymous`` lines of ``/proc/<pid>/smaps_rollup``,
    or of ``smaps`` under gVisor, which has no rollup, summed. None when it is gone or unreadable.

    Roughly what stopping the process frees. Under gVisor ``VmRSS`` counts the whole span of every
    mapping the process has touched, so it reads up to ten times higher for an agent's harness,
    which maps a large binary.
    """
    try:
        return _summed_anonymous_kib(
            (proc / str(pid) / "smaps_rollup").read_text(errors="replace")
        )
    except OSError:
        pass
    memory = read_smaps_memory(pid, proc)
    return None if memory is None else memory.anonymous_kib


# A mapping's header line is "<start>-<end> <perms> <offset> <device> <inode> [<path>]"; a shared
# memory segment's path is a memfd, a /dev/shm file, or a System V segment. Its field lines run up
# to the next header.
_SHARED_MAPPING: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]+-[0-9a-f]+ \S+ \S+ (?P<device>\S+) (?P<inode>\d+) +(?:/memfd:|/dev/shm/|/SYSV)[^\n]*\n"
    r"(?P<fields>(?:(?![0-9a-f]+-[0-9a-f]+ )[^\n]*\n)*)",
    re.MULTILINE,
)
_RSS_LINE: Final[re.Pattern[str]] = re.compile(r"^Rss:\s+(\d+)", re.MULTILINE)
_ANONYMOUS_LINE: Final[re.Pattern[str]] = re.compile(
    r"^Anonymous:\s+(\d+)", re.MULTILINE
)


def _summed_anonymous_kib(text: str) -> int:
    # A regex rather than a line loop: under gVisor a whole-workspace pass reads hundreds of
    # thousands of smaps lines.
    return sum(int(kib) for kib in _ANONYMOUS_LINE.findall(text))
