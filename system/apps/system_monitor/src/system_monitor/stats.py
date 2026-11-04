"""Everything the overview shows, gathered into one snapshot that every open window shares.

The readers live beside this module: ``memory`` (memory in use, disk), ``programs`` (the running
programs and who they belong to), ``usage`` (the breakdown's rows), ``stops`` (what the memory
guard stopped), and ``cpu`` (the resource sampler's readings, the graphs' history, and the busy
stretches). What the rows' Stop buttons do is ``actions``.
"""

import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oom_priority.meminfo import read_meminfo
from oom_priority.paths import shed_ledger_path
from oom_priority.supervisor_rpc import supervised_pids

from system_monitor import cpu, memory, programs, stops, usage

WORKSPACE_DIR = Path("/home/user/workspace")


def snapshot(
    recent_path: Path = cpu.RECENT_PATH,
    stretches_path: Path = cpu.STRETCHES_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    procs = programs.read_processes()
    chats = programs.chats()
    names = {agent_id: chat.name for agent_id, chat in chats.items()}
    chat_ids = {agent_id: chat.chat_id for agent_id, chat in chats.items()}
    meminfo = read_meminfo()
    apps, supervised = programs.user_apps(), supervised_pids()
    agents = programs.agents_by_pid()
    program_names = programs.program_names(supervised)
    scratch = memory.disk(usage.SCRATCH_DIR)
    # Scratch space is memory only where /tmp is a tmpfs; elsewhere its files are on disk.
    scratch_memory = scratch if usage.scratch_in_memory() else None
    breakdown = (
        usage.exact_usage if programs.has_breakdown(procs) else usage.scaled_usage
    )
    reading = cpu.read_recent(recent_path, moment.timestamp())
    stop_records = stops.recent_stops(shed_ledger_path(), now=moment, names=names)
    events = [
        {
            "at": stop["at"],
            "kind": "stop",
            "what": f"Stopped {stop['what']} to free memory",
            "duration_seconds": None,
            "top": [],
        }
        for stop in stop_records
    ] + cpu.recent_stretches(stretches_path, moment, chat_names=names, app_names=apps)
    events.sort(key=lambda event: event["at"], reverse=True)
    return {
        "cpu": {
            "usage": reading.usage if reading else 0.0,
            "cores": reading.cores if reading else 1,
            "processes": len(procs),
            "sampled_at": reading.sampled_at if reading else None,
        },
        "memory": meminfo,
        "disk": memory.disk(WORKSPACE_DIR),
        "usage": breakdown(
            procs,
            names,
            apps,
            supervised,
            meminfo["used"],
            scratch_memory,
            agents,
            reading.by_pid if reading else None,
            chat_ids=chat_ids,
            program_names=program_names,
            cpu_usage=reading.usage if reading else None,
        ),
        # "exact": rows are anonymous memory plus Shared and System; "scaled": rows are resident
        # sizes shrunk by one factor, then System for the rest (no smaps_rollup on this kernel).
        # The page words its note by it.
        "usage_mode": "exact" if breakdown is usage.exact_usage else "scaled",
        "stops": stop_records,
        "events": events,
        "history": reading.samples if reading else [],
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
