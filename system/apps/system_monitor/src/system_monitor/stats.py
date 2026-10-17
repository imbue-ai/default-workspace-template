"""Everything the page shows, gathered into one snapshot that every open window shares.

The readers live beside this module: ``memory`` (memory in use, disk, the graph's history),
``programs`` (the running programs and who they belong to), ``usage`` (the breakdown's rows),
and ``stops`` (what the memory guard stopped).
"""

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from oom_priority.paths import shed_ledger_path

from system_monitor import memory, programs, stops, usage

WORKSPACE_DIR = Path("/home/user/workspace")


def snapshot() -> dict[str, Any]:
    procs = programs.read_processes()
    names = programs.chat_names()
    meminfo = memory.read_meminfo()
    apps, supervised = programs.user_apps(), programs.supervised_pids()
    agents = programs.agents_by_pid()
    scratch = memory.disk(usage.SCRATCH_DIR)
    # Scratch space is memory only where /tmp is a tmpfs; elsewhere its files are on disk.
    scratch_memory = scratch if usage.scratch_in_memory() else None
    breakdown = (
        usage.exact_usage if programs.has_breakdown(procs) else usage.scaled_usage
    )
    return {
        "memory": meminfo,
        "disk": memory.disk(WORKSPACE_DIR),
        "usage": breakdown(
            procs, names, apps, supervised, meminfo["used"], scratch_memory, agents
        ),
        # "exact": rows are private memory plus Shared and System; "scaled": rows are resident
        # sizes shrunk by one factor (no smaps_rollup on this kernel). The page words its note by it.
        "usage_mode": "exact" if breakdown is usage.exact_usage else "scaled",
        "stops": stops.recent_stops(shed_ledger_path(), names=names),
    }


class SharedSnapshot:
    """One snapshot shared by every window: computed at most once per ``max_age`` seconds.

    Each open window polls every five seconds; without this, every window would read every
    process's memory on its own. A request inside ``max_age`` of the last computation gets that
    result, and requests that arrive while one is being computed wait for it.
    """

    def __init__(
        self,
        compute: Callable[[], dict[str, Any]],
        max_age: float = 4.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._compute = compute
        self._max_age = max_age
        self._clock = clock
        self._lock = threading.Lock()
        self._value: dict[str, Any] | None = None
        self._taken = 0.0

    def get(self) -> dict[str, Any]:
        with self._lock:
            if self._value is None or self._clock() - self._taken >= self._max_age:
                self._value = self._compute()
                self._taken = self._clock()
            return self._value
