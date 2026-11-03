"""The agent-pid registry: maps an agent's main process pid to its identity.

earlyoom's after-kill hook is handed only the killed pid, uid, and process name
(comm) -- and by then the process is gone, so it can't inspect ``/proc`` to learn
which agent it was. An agent's main process is a harness process whose comm
("claude"/"codex"/"pi"/"node") does not reveal the agent name. So each agent records
its own main-process pid here at launch (the launch wrapper,
``system/services/oom_priority/bin/agent_oom_launch.py``, calls ``record_agent_pid``
for its own pid just before it execs the harness); the kill hook looks the killed pid
up to decide whether an *agent* was shed (which drives revival) versus a mere
subprocess.

One file per pid (``<pid>.json``) rather than a shared map, so concurrent agents
never race on a single file and no locking is needed. An entry outlives its
process until the next ``record_agent_pid`` prunes it, and by then its pid may
belong to something else: under gVisor ``pid_max`` is 65536, so pids come round
within hours, and ``/proc/<id>`` also resolves for a thread of an unrelated
process. Each entry therefore records its process's start time and the boot it
was read in (the registry lives on the volume, so it outlives a restart), and a
reader counts an entry only while the pid names a process (not a thread) with
that start time in this boot. A re-tag through a stale entry would otherwise
land on whatever holds the pid now, and a write to a thread's ``oom_score_adj``
re-bands its whole process. The kill hook reads an entry after its process is
gone, so it can check only the boot.

One agent can register more than one pid: codex runs both a visible ``--remote`` TUI
and an ``app-server`` daemon from the same launch command, so both are recorded under
its id, and ``lookup_pids_by_agent_id`` returns them all.

Stdlib-only (see ``paths``): imported by the launch wrapper and the kill hook
under a plain ``python3``.
"""

import json
from pathlib import Path

from oom_priority.paths import agent_pids_dir
from oom_priority.proctree import (
    is_thread,
    read_boot_id,
    read_environ_value,
    read_start_ticks,
)

_PROC_DIR = Path("/proc")
_START_TICKS_KEY = "start_ticks"
_BOOT_ID_KEY = "boot_id"
_AGENT_ID_ENV_VAR = "MNGR_AGENT_ID"


def _entry_path(pid: int) -> Path:
    return agent_pids_dir() / f"{pid}.json"


def _is_live_process(pid: int, proc_dir: Path) -> bool:
    """Whether ``pid`` is a running process, as opposed to gone or another process's thread."""
    return (proc_dir / str(pid)).is_dir() and not is_thread(pid, proc_dir)


def _has_process_identity(record: dict) -> bool:
    return isinstance(record.get(_START_TICKS_KEY), int)


def _is_recorded_process(pid: int, record: dict, proc_dir: Path) -> bool:
    """Whether ``pid`` still names the process ``record`` was written for.

    It must be a live process that started at the recorded time in this boot, and
    this boot's id must be readable: an unknown boot cannot rule out an earlier one.
    """
    if not _is_live_process(pid, proc_dir):
        return False
    # CLEANUP: an entry with no start time was written before they were recorded, so
    # it counts only while its pid is a process of the agent it names. Its agent may
    # run for days before it relaunches and records its start time. Drop this branch
    # one release after start times shipped, once every such agent has relaunched.
    if not _has_process_identity(record):
        agent_id = record.get("agent_id")
        return (
            isinstance(agent_id, str)
            and bool(agent_id)
            and read_environ_value(pid, _AGENT_ID_ENV_VAR, proc_dir) == agent_id
        )
    boot_id = read_boot_id(proc_dir)
    is_same_start = read_start_ticks(pid, proc_dir) == record[_START_TICKS_KEY]
    return is_same_start and boot_id is not None and record.get(_BOOT_ID_KEY) == boot_id


def _is_current_entry(pid: int, record: dict | None, proc_dir: Path) -> bool:
    """Whether a prune keeps the entry: it names its process, or might.

    An unreadable entry may be another agent's record mid-write, so it is kept
    while its pid is a live process.
    """
    if record is None:
        return _is_live_process(pid, proc_dir)
    return _is_recorded_process(pid, record, proc_dir)


def _read_entries(directory: Path) -> list[tuple[Path, int, dict | None]]:
    """Every ``<pid>.json`` in the registry, with its record (None when unreadable)."""
    if not directory.is_dir():
        return []
    entries: list[tuple[Path, int, dict | None]] = []
    for entry in directory.iterdir():
        if entry.suffix != ".json" or not entry.stem.isdigit():
            continue
        try:
            data = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            data = None
        entries.append(
            (entry, int(entry.stem), data if isinstance(data, dict) else None)
        )
    return entries


def record_agent_pid(
    pid: int,
    agent_name: str,
    is_worker: bool,
    agent_id: str | None = None,
    proc_dir: Path = _PROC_DIR,
) -> None:
    """Register ``pid`` as the main process of ``agent_name``, with its start time and boot.

    ``agent_id`` (mngr's stable per-agent id) is recorded alongside the name so a
    consumer that knows only the id -- e.g. the chat app's OOM prioritizer, which
    re-tags a chat by id -- can resolve the live pids via
    ``lookup_pids_by_agent_id``. It is optional so older callers/tests that pass
    only a name still work.

    Overwrites any prior entry for the same pid (a reused pid), and prunes
    entries that no longer name their process so the directory does not grow
    without bound across the container's life.
    """
    directory = agent_pids_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # Prune BEFORE writing so a stale entry can never be mistaken for clearing
    # the entry we are about to add.
    prune_stale_entries(proc_dir)
    record: dict[str, object] = {"agent_name": agent_name, "is_worker": is_worker}
    if agent_id is not None:
        record["agent_id"] = agent_id
    start_ticks = read_start_ticks(pid, proc_dir)
    if start_ticks is not None:
        record[_START_TICKS_KEY] = start_ticks
        record[_BOOT_ID_KEY] = read_boot_id(proc_dir)
    _entry_path(pid).write_text(json.dumps(record))


def lookup_agent(pid: int, proc_dir: Path = _PROC_DIR) -> dict | None:
    """Return ``{"agent_name", "is_worker"}`` for ``pid``, or None if not an
    agent main process (or the entry is missing/unreadable, or from an earlier boot).

    Called after the process is gone, so only its boot can be checked: an entry
    left from before a restart likely names an early pid the new boot reuses. An
    entry with no boot id, or a boot id that cannot be read now, is trusted, since
    dropping it would lose a real shed record."""
    try:
        data = json.loads(_entry_path(pid).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "agent_name" not in data:
        return None
    recorded_boot_id = data.get(_BOOT_ID_KEY)
    if isinstance(recorded_boot_id, str):
        boot_id = read_boot_id(proc_dir)
        if boot_id is not None and boot_id != recorded_boot_id:
            return None
    return data


def live_pids_by_agent_id(proc_dir: Path = _PROC_DIR) -> dict[str, list[int]]:
    """Every registered pid that still names its process, grouped by the ``agent_id`` recorded with it.

    An agent can hold more than one (codex's TUI and its app-server daemon). Entries
    recorded without an ``agent_id``, unreadable ones, and ones whose pid no longer
    names the recorded process are left out.
    """
    pids_by_agent_id: dict[str, list[int]] = {}
    for _entry, pid, data in _read_entries(agent_pids_dir()):
        if data is None:
            continue
        agent_id = data.get("agent_id")
        if (
            isinstance(agent_id, str)
            and agent_id
            and _is_recorded_process(pid, data, proc_dir)
        ):
            pids_by_agent_id.setdefault(agent_id, []).append(pid)
    return pids_by_agent_id


def lookup_pids_by_agent_id(agent_id: str, proc_dir: Path = _PROC_DIR) -> list[int]:
    """Every live main-process pid recorded for ``agent_id``, sorted; empty when none.

    So a consumer holding only the id (the OOM prioritizer) can re-tag each of the
    agent's processes. Empty for a dormant chat with no running process, an id
    recorded before ``agent_id`` was captured, or entries whose pid no longer names
    the recorded process.
    """
    return sorted(live_pids_by_agent_id(proc_dir).get(agent_id, []))


def prune_stale_entries(proc_dir: Path = _PROC_DIR) -> None:
    """Remove registry entries that no longer name their process: exited, reused, or from an earlier boot."""
    for entry, pid, data in _read_entries(agent_pids_dir()):
        if _is_current_entry(pid, data, proc_dir):
            continue
        try:
            entry.unlink()
        except OSError:
            pass
