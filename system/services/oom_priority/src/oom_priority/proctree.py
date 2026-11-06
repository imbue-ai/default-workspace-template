"""Reading processes through ``/proc``: the live tree under a pid, and what identifies one process.

The tree walk is used by the tagging backstops that must find the processes a service has
already spawned: the supervisord event listener (``system/services/oom_priority/bin/oom_tag_backstop.py``)
and the browser service's Chromium re-tagging sweep. The identity readers are used by the
agent-pid registry, which must tell the process it recorded from a later one that reused its
pid. Stdlib-only (see ``paths``): the backstop listener imports it under a plain ``python3``.
"""

from pathlib import Path

_PROC_DIR = Path("/proc")


def list_descendant_pids(pid: int, proc_dir: Path = _PROC_DIR) -> list[int]:
    """All current descendants of ``pid``, via ``/proc/<pid>/task/*/children``.

    Best-effort: a process that exits mid-walk is skipped, and on a host without
    ``/proc`` (e.g. macOS) the result is empty. The ``seen`` guard makes the walk
    terminate even on an inconsistent snapshot of a changing process tree. gVisor
    lists a child process's threads in ``children`` beside the process itself; a
    thread is left out, since its process is listed and walked in its own right.
    """
    seen: set[int] = {pid}
    frontier = [pid]
    descendants: list[int] = []
    while frontier:
        current = frontier.pop()
        task_dir = proc_dir / str(current) / "task"
        try:
            tasks = list(task_dir.iterdir())
        except OSError:
            continue
        for task in tasks:
            try:
                children_text = (task / "children").read_text()
            except OSError:
                continue
            for child_text in children_text.split():
                if not child_text.isdigit() or int(child_text) in seen:
                    continue
                child = int(child_text)
                seen.add(child)
                if is_thread(child, proc_dir):
                    continue
                descendants.append(child)
                frontier.append(child)
    return descendants


def is_thread(task_id: int, proc_dir: Path = _PROC_DIR) -> bool:
    """Whether ``/proc`` says ``task_id`` is a thread of another process (its Tgid is
    not its own id); False when its status cannot be read.

    ``/proc/<tid>`` resolves for a thread although ``/proc`` does not list it, so a path's
    existence alone does not make an id a process."""
    try:
        status = (proc_dir / str(task_id) / "status").read_text(errors="replace")
    except OSError:
        return False
    for line in status.splitlines():
        key, _, value = line.partition(":")
        if key == "Tgid":
            return value.strip() != str(task_id)
    return False


def read_boot_id(proc_dir: Path = _PROC_DIR) -> str | None:
    """This boot's random id (``/proc/sys/kernel/random/boot_id``), or None when unreadable.

    A start time counts from boot, so it identifies a process only together with the boot it
    was read in; a file that outlives a restart needs both.
    """
    try:
        return (
            proc_dir / "sys" / "kernel" / "random" / "boot_id"
        ).read_text().strip() or None
    except OSError:
        return None


def read_start_ticks(pid: int, proc_dir: Path = _PROC_DIR) -> int | None:
    """When ``pid`` started, in clock ticks since boot (field 22 of ``/proc/<pid>/stat``); None
    when it is gone or its stat cannot be parsed.

    Under gVisor ``pid_max`` is 65536, so a pid is reused within hours of its process exiting,
    but two processes that held the same pid never share a start time. The fields are counted
    from after the last ``)``, since the command name before it may hold spaces or parentheses,
    and it may be any bytes, so they are decoded leniently.
    """
    try:
        stat = (proc_dir / str(pid) / "stat").read_text(errors="replace")
    except OSError:
        return None
    fields = stat.rpartition(")")[2].split()
    # fields[0] is field 3 (state), so field 22 (starttime) is fields[19].
    if len(fields) < 20 or not fields[19].isdigit():
        return None
    return int(fields[19])


def read_environ_value(pid: int, name: str, proc_dir: Path = _PROC_DIR) -> str | None:
    """The value ``pid`` was started with for environment variable ``name`` (from
    ``/proc/<pid>/environ``); None when it has none, is gone, or its environment cannot be read."""
    try:
        environ = (proc_dir / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    prefix = f"{name}=".encode()
    for entry in environ.split(b"\0"):
        if entry.startswith(prefix):
            return entry[len(prefix) :].decode(errors="replace")
    return None
