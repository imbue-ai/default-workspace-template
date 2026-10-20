"""Memory in use and disk space, and the memory graph's last 10 minutes.

Memory figures come from the same place the memory guard (earlyoom) takes them: the
workspace's own headroom file while it is fresh, else /proc/meminfo.
"""

import collections
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

from oom_priority.cgroup_meminfo import HOST_MEMINFO_PATH

PROC = Path("/proc")
KIB = 1024
# earlyoom (--host-meminfo, system/supervisord.conf.d/earlyoom.conf) ignores the headroom file
# once it is older than this.
HOST_MEMINFO_MAX_AGE_SECONDS = 5.0
# How often the graph gets a reading, and how far back it reaches.
SAMPLE_EVERY_SECONDS = 5.0
HISTORY_WINDOW_SECONDS = 600.0


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
    """Total, available, and used memory in bytes: the figures the memory guard acts on.

    Under docker the workspace publishes its cgroup's limit and headroom to ``host_meminfo``,
    and under gVisor the gen-2 slice VM does; there /proc/meminfo describes the whole host or
    misses what the runtime is charged. earlyoom reads the file while it is fresh, and so does
    this, falling back to /proc/meminfo.
    """
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


def disk(path: Path) -> dict[str, int]:
    usage = shutil.disk_usage(path)
    return {"total": usage.total, "used": usage.used, "free": usage.free}


class MemoryHistory:
    """The share of memory in use over the last ``window`` seconds, for the page's graph.

    Kept by the app, so the line stays whole while a window is hidden and across reloads. Each
    reading is one read of the memory totals; the per-process breakdown is not part of it.
    """

    def __init__(
        self,
        read: Callable[[], dict[str, int]] = read_meminfo,
        clock: Callable[[], float] = time.time,
        window: float = HISTORY_WINDOW_SECONDS,
    ) -> None:
        self._read = read
        self._clock = clock
        self._window = window
        self._lock = threading.Lock()
        self._samples: collections.deque[tuple[int, float]] = collections.deque()

    def record(self) -> None:
        memory = self._read()
        now_ms = int(self._clock() * 1000)
        with self._lock:
            self._samples.append((now_ms, memory["used"] / memory["total"]))
            while self._samples and now_ms - self._samples[0][0] > self._window * 1000:
                self._samples.popleft()

    def samples(self) -> list[dict[str, float]]:
        """Oldest first, each ``{"t": epoch milliseconds, "v": fraction in use}``."""
        with self._lock:
            return [{"t": t, "v": v} for t, v in self._samples]


def start_sampler(
    history: MemoryHistory, every: float = SAMPLE_EVERY_SECONDS
) -> threading.Event:
    """Record a reading now and then every ``every`` seconds on a daemon thread, until the
    returned event is set. A reading that fails is skipped."""
    history.record()
    stop = threading.Event()

    def run() -> None:
        while not stop.wait(every):
            try:
                history.record()
            except (OSError, KeyError, ZeroDivisionError):
                continue

    threading.Thread(target=run, name="memory-history", daemon=True).start()
    return stop
