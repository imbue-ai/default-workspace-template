"""What is using the memory: the rows of the page's breakdown.

Programs share memory: those that load the same files (the Python and Node runtimes, Chromium,
system libraries) map the same pages, and each one's resident size (RSS) counts those pages in
full. Where the kernel reports each program's own and proportional memory
(``/proc/<pid>/smaps_rollup``), ``exact_usage`` splits the memory in use without estimating:
each group holds its programs' private memory (what stopping them frees), the shared pages are
counted once in a Shared row, the files in the /tmp scratch space are a Temporary files row,
and what no program holds (the kernel, programs this app cannot inspect) is the System row.

Where the kernel has no smaps_rollup (gVisor), only RSS is available, and ``scaled_usage``
shrinks every group by one factor until they add up to what is in use. Checked against real
stops, a 540 MB chat freed about 245 MB and 950 MB of code checkers freed about 630 MB, so these
rows are a rough guide.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from system_monitor.memory import PROC
from system_monitor.programs import Process, chat_agent_id, is_code_checker

SCRATCH_DIR = Path("/tmp")


def _group(gid: str, label: str, parts: dict[str, int]) -> dict[str, Any]:
    # Largest first; sorted on the (name, bytes) pairs so the sort key stays a plain int.
    items = [
        {"name": k, "bytes": v}
        for k, v in sorted(parts.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return {
        "id": gid,
        "label": label,
        "hint": "",
        "bytes": sum(parts.values()),
        "items": items,
    }


def _total_row(gid: str, label: str, amount: int, hint: str = "") -> dict[str, Any]:
    """A row with only a total, no items under it."""
    return {"id": gid, "label": label, "hint": hint, "bytes": amount, "items": []}


def _private(p: Process) -> int:
    return p.private or 0


def group_usage(
    procs: dict[int, Process],
    names: dict[str, str],
    apps: dict[str, str],
    supervised: dict[str, int],
    size: Callable[[Process], int] = lambda p: p.rss,
    agents: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Split the memory programs hold into the groups the page shows, counting ``size`` of each.

    A chat is every process an agent's main process started, the main processes being the
    registered ones (``agents``, any harness) and, failing that, claude found by its command line.
    """
    chats: dict[str, int] = {}
    checkers: dict[str, int] = {}
    app_mem: dict[str, int] = {}
    other = 0
    chat_pids = {
        pid: aid
        for pid, p in procs.items()
        if p.argv
        and Path(p.argv[0]).name == "claude"
        and (aid := chat_agent_id(" ".join(p.argv)))
    }
    chat_pids.update({pid: aid for pid, aid in (agents or {}).items() if pid in procs})
    app_roots = {pid: apps[prog] for prog, pid in supervised.items() if prog in apps}

    def owner(pid: int) -> tuple[str, str] | None:
        seen = 0
        while pid in procs and pid > 1 and seen < 64:
            if pid in chat_pids:
                return "chat", chat_pids[pid]
            if pid in app_roots:
                return "app", app_roots[pid]
            pid = procs[pid].ppid
            seen += 1
        return None

    for pid, p in procs.items():
        amount = size(p)
        if not amount:
            continue
        who = owner(pid)
        chat = names.get(who[1], who[1]) if who and who[0] == "chat" else None
        if is_code_checker(" ".join(p.argv[:3])):
            key = chat if chat is not None else "Other"
            checkers[key] = checkers.get(key, 0) + amount
        elif chat is not None:
            # the chat itself and whatever it is running right now (a test run, a browser)
            chats[chat] = chats.get(chat, 0) + amount
        elif who:
            app_mem[who[1]] = app_mem.get(who[1], 0) + amount
        else:
            other += amount

    return [
        _group("chats", "Chats", chats),
        _group("checkers", "Code checkers", checkers),
        # Everything not a chat, a code checker, or a user-built app: the built-in apps (this
        # one included) land here, since only the user band's apps get rows of their own.
        _total_row(
            "workspace",
            "Workspace",
            other,
            "chat app, desktop, built-in apps, background services",
        ),
        _group("apps", "Your apps", app_mem),
    ]


def scratch_in_memory(mounts: Path = PROC / "mounts", path: Path = SCRATCH_DIR) -> bool:
    """Whether the scratch space (/tmp) is a tmpfs, whose files are held in memory.

    It is in every workspace today (the docker and cloud providers mount one; Debian's own
    tmp.mount does under lima). Where it is not, its files are on disk and use no memory.
    """
    try:
        lines = mounts.read_text().splitlines()
    except OSError:
        return False
    is_tmpfs = False
    for line in lines:
        fields = line.split()
        if len(fields) >= 3 and fields[1] == str(path):
            # The last mount on the path is the one in effect.
            is_tmpfs = fields[2] == "tmpfs"
    return is_tmpfs


def _scratch_row(scratch: dict[str, int]) -> dict[str, Any]:
    # Its limit rides along so the page can say "of 1.9 GB limit".
    return {
        **_total_row("scratch", "Temporary files", scratch["used"]),
        "limit": scratch["total"],
    }


def exact_usage(
    procs: dict[int, Process],
    names: dict[str, str],
    apps: dict[str, str],
    supervised: dict[str, int],
    used: int,
    scratch: dict[str, int] | None = None,
    agents: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """The groups by private memory, then Shared, Temporary files, and System rows, adding up to ``used``.

    Shared is every page mapped by more than one process, counted once: the sum over
    processes of their proportional share less their private memory. Scratch space is the
    files in /tmp when it is held in memory (``scratch``, None when it is not). System is the
    rest of the memory in use: what no process holds (the kernel) plus the processes this app
    cannot inspect. It cannot go below zero; the file pages a program maps are partly counted
    as available, so the rows can sum a little past ``used``.
    """
    groups = group_usage(procs, names, apps, supervised, size=_private, agents=agents)
    shared = sum(
        p.pss - p.private
        for p in procs.values()
        if p.pss is not None and p.private is not None
    )
    groups.append(_total_row("shared", "Shared", shared, "by chats, apps and services"))
    if scratch is not None:
        groups.append(_scratch_row(scratch))
    held = sum(g["bytes"] for g in groups)
    groups.append(_total_row("system", "System", max(0, used - held)))
    return groups


def scaled_usage(
    procs: dict[int, Process],
    names: dict[str, str],
    apps: dict[str, str],
    supervised: dict[str, int],
    used: int,
    scratch: dict[str, int] | None = None,
    agents: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """The groups by resident size, shrunk to the memory in use less the scratch space's, then a
    Temporary files row (when /tmp is held in memory): the fallback without smaps_rollup."""
    in_scratch = scratch["used"] if scratch is not None else 0
    groups = scale_to_used(
        group_usage(procs, names, apps, supervised, agents=agents),
        max(0, used - in_scratch),
    )
    if scratch is not None:
        groups.append(_scratch_row(scratch))
    return groups


def scale_to_used(groups: list[dict[str, Any]], used: int) -> list[dict[str, Any]]:
    """Shrink every figure by one factor so the groups add up to the memory in use."""
    counted = sum(g["bytes"] for g in groups)
    factor = min(1.0, used / counted) if counted else 1.0
    for g in groups:
        g["bytes"] = int(g["bytes"] * factor)
        for item in g["items"]:
            item["bytes"] = int(item["bytes"] * factor)
    return groups
