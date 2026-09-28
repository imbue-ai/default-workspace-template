"""Walking the live process tree under a pid, via ``/proc``.

Used by the tagging backstops that must find the processes a service has
already spawned: the supervisord event listener (``system/services/oom_priority/bin/oom_tag_backstop.py``)
and the browser service's Chromium re-tagging sweep. Stdlib-only (see ``paths``):
the backstop listener imports it under a plain ``python3``.
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
                if _is_thread(child, proc_dir):
                    continue
                descendants.append(child)
                frontier.append(child)
    return descendants


def _is_thread(task_id: int, proc_dir: Path) -> bool:
    """Whether ``/proc`` says ``task_id`` is a thread of another process (its Tgid is
    not its own id); False when its status cannot be read."""
    try:
        status = (proc_dir / str(task_id) / "status").read_text()
    except OSError:
        return False
    for line in status.splitlines():
        key, _, value = line.partition(":")
        if key == "Tgid":
            return value.strip() != str(task_id)
    return False
