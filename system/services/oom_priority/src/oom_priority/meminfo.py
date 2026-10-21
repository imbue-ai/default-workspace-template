"""Memory in use, read where the memory guard reads it.

Under docker the workspace publishes its cgroup's limit and headroom to the
headroom file, and under gVisor the gen-2 slice VM does; there ``/proc/meminfo``
describes the whole host or misses what the runtime is charged. earlyoom reads
the file while it is fresh (``--host-meminfo``, ``earlyoom.conf``), and so does
this, falling back to ``/proc/meminfo``. The resource sampler records memory
through it, and the system monitor app shows the same figures.

Stdlib-only (see ``paths``): the sampler runs under a plain ``python3``.
"""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

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


def read_meminfo(
    proc: Path = PROC,
    host_meminfo: Path = HOST_MEMINFO_PATH,
    clock: Callable[[], float] = time.time,
) -> dict[str, int]:
    """Total, available, and used memory in bytes: the figures the memory guard acts on."""
    values: dict[str, int] = {}
    try:
        if clock() - host_meminfo.stat().st_mtime <= HOST_MEMINFO_MAX_AGE_SECONDS:
            values = parse_kib_fields(host_meminfo.read_text())
    except OSError:
        values = {}
    if "MemTotal" not in values or "MemAvailable" not in values:
        values = parse_kib_fields((proc / "meminfo").read_text())
    total = values["MemTotal"]
    available = values.get("MemAvailable", values.get("MemFree", 0))
    return {"total": total, "available": available, "used": total - available}
