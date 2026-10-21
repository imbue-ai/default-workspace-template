"""What is using the memory and the CPU: the rows of the page's breakdown.

Programs share memory: those that load the same files (the Python and Node runtimes, Chromium,
system libraries) map the same pages, and each one's resident size (RSS) counts those pages in
full. Where the kernel reports each program's own memory (``/proc/<pid>/smaps_rollup``),
``exact_usage`` splits the memory in use without estimating: each group holds its programs'
anonymous memory (what they made for themselves, which is what stopping them frees; the file
pages they map are cache the kernel drops under pressure, and the memory guard's figure of what
is in use leaves them out too), the shared memory segments programs map are counted once in a
Shared row, the files in the /tmp scratch space are a Temporary files row, and what no program
holds (the kernel, programs this app cannot inspect) is the System row.

Where the kernel has no smaps_rollup (gVisor), only RSS is available, and ``scaled_usage``
shrinks every group by one factor until they add up to what is in use. Checked against real
stops, a 540 MB chat freed about 245 MB and 950 MB of code checkers freed about 630 MB, so these
rows are a rough guide.

Each row's CPU is the sum of the sampler's per-process shares over its processes; the System row
takes whatever share the rows together fall short of the whole, so the column adds up.
"""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from oom_priority.meminfo import PROC

from system_monitor.programs import Process, chat_agent_id, is_code_checker

SCRATCH_DIR = Path("/tmp")
# The item for a code checker no chat started, and for a process no chat, app or program owns
# whose command is not in the table below.
OTHER_ITEM = "Other"
# Where a process no chat, app or program started is shown, by the name of its command: the
# SSH daemon and its sessions, and the machinery that boots and holds the workspace together
# (tini, the bootstrap and its uv wrapper, supervisord, the services agent's tmux and shell).
SSH_ITEM = "SSH access"
INTERNAL_ITEM = "Internal system"
UNOWNED_ITEMS = {
    "sshd": SSH_ITEM,
    "sshd-session": SSH_ITEM,
    "tini": INTERNAL_ITEM,
    "uv": INTERNAL_ITEM,
    "bootstrap": INTERNAL_ITEM,
    "supervisord": INTERNAL_ITEM,
    "tmux": INTERNAL_ITEM,
    "bash": INTERNAL_ITEM,
    "sh": INTERNAL_ITEM,
    "sleep": INTERNAL_ITEM,
}
# How far up a ppid chain to look for an owner before giving up.
_MAX_ANCESTRY = 64

# Who a process belongs to: ("chat", agent id), ("app", display name) or ("program", display name).
Owner = tuple[str, str]


def _group(
    gid: str,
    label: str,
    parts: dict[str, int],
    cpu_parts: dict[str, float] | None,
    stops: Mapping[str, dict[str, str]] | None = None,
    hint: str = "",
) -> dict[str, Any]:
    """A row with an item per name, largest memory first; CPU shares and stop targets ride along."""
    items = [
        {
            "name": k,
            "bytes": v,
            "cpu": None if cpu_parts is None else round(cpu_parts.get(k, 0.0), 4),
            "stop": (stops or {}).get(k),
        }
        for k, v in sorted(parts.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return {
        "id": gid,
        "label": label,
        "hint": hint,
        "bytes": sum(parts.values()),
        "cpu": None if cpu_parts is None else round(sum(cpu_parts.values()), 4),
        "items": items,
    }


def _total_row(
    gid: str, label: str, amount: int, hint: str = "", cpu: float | None = None
) -> dict[str, Any]:
    """A row with only a total, no items under it."""
    return {
        "id": gid,
        "label": label,
        "hint": hint,
        "bytes": amount,
        "cpu": cpu,
        "items": [],
    }


def unowned_item(p: Process) -> str:
    """The Workspace item a process nothing started belongs under: by its command, else Other.

    The command is the first word of its first argument (sshd rewrites its own to ``sshd: ...``),
    or the script a Python interpreter runs (supervisord is ``python3 /usr/bin/supervisord``).
    """
    words = p.argv[0].split() if p.argv else []
    command = Path(words[0]).name.rstrip(":") if words else ""
    if command.startswith("python") and len(p.argv) > 1:
        command = Path(p.argv[1]).name
    return UNOWNED_ITEMS.get(command, OTHER_ITEM)


def _anonymous(p: Process) -> int:
    # A kernel that reports Pss but no Anonymous line (before 4.14) falls back to private memory.
    return p.anonymous if p.anonymous is not None else (p.private or 0)


def chat_pids(
    procs: Mapping[int, Process], agents: Mapping[int, str] | None
) -> dict[int, str]:
    """pid -> agent id for every chat's main process: the registered ones (any harness) and,
    failing that, claude found by its command line."""
    pids = {
        pid: aid
        for pid, p in procs.items()
        if p.argv
        and Path(p.argv[0]).name == "claude"
        and (aid := chat_agent_id(" ".join(p.argv)))
    }
    pids.update({pid: aid for pid, aid in (agents or {}).items() if pid in procs})
    return pids


def _find_owner(
    pid: int,
    procs: Mapping[int, Process],
    chats: Mapping[int, str],
    apps: Mapping[int, str],
    programs: Mapping[int, str],
) -> Owner | None:
    """The chat, app or program ``pid`` descends from, walking its parents; None when no root claims it."""
    for _ in range(_MAX_ANCESTRY):
        if pid not in procs or pid <= 1:
            return None
        if pid in chats:
            return "chat", chats[pid]
        if pid in apps:
            return "app", apps[pid]
        if pid in programs:
            return "program", programs[pid]
        pid = procs[pid].ppid
    return None


def checker_pids_of_chat(
    procs: Mapping[int, Process], agents: Mapping[int, str] | None, agent_id: str
) -> list[int]:
    """The code checkers a chat's agent started, by pid."""
    chats = chat_pids(procs, agents)
    return [
        pid
        for pid, p in procs.items()
        if is_code_checker(" ".join(p.argv[:3]))
        and _find_owner(pid, procs, chats, {}, {}) == ("chat", agent_id)
    ]


def group_usage(
    procs: dict[int, Process],
    names: dict[str, str],
    apps: dict[str, str],
    supervised: dict[str, int],
    size: Callable[[Process], int] = lambda p: p.rss,
    agents: dict[int, str] | None = None,
    cpu_by_pid: Mapping[int, float] | None = None,
    chat_ids: Mapping[str, str] | None = None,
    program_names: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Split the memory programs hold into the groups the page shows, counting ``size`` of each, and the CPU
    beside it when the sampler's per-process shares (``cpu_by_pid``) are at hand.

    A chat is every process an agent's main process started, the main processes being the
    registered ones (``agents``, any harness) and, failing that, claude found by its command line.
    A process's CPU share includes the children it has reaped, so it lands with the group of
    whatever started it. The workspace's own programs (``program_names``: supervised program ->
    the name shown) are the items of the Workspace row; what none of them started is "SSH access",
    "Internal system" or "Other" by its command (``unowned_item``).
    Chats, checkers and apps carry the target their Stop button acts on: the chat's id
    (``chat_ids``: agent id -> chat id), the agent whose checkers, or the app's program.
    """
    chats: dict[str, int] = {}
    checkers: dict[str, int] = {}
    programs_mem: dict[str, int] = {}
    app_mem: dict[str, int] = {}
    chats_cpu: dict[str, float] = {}
    checkers_cpu: dict[str, float] = {}
    programs_cpu: dict[str, float] = {}
    app_cpu: dict[str, float] = {}
    chat_stops: dict[str, dict[str, str]] = {}
    checker_stops: dict[str, dict[str, str]] = {}
    app_stops: dict[str, dict[str, str]] = {}
    chat_roots = chat_pids(procs, agents)
    app_roots = {pid: apps[prog] for prog, pid in supervised.items() if prog in apps}
    app_programs = {apps[prog]: prog for prog in supervised if prog in apps}
    program_roots = {
        pid: (program_names or {}).get(prog, prog)
        for prog, pid in supervised.items()
        if prog not in apps
    }

    for pid, p in procs.items():
        amount = size(p)
        share = cpu_by_pid.get(pid, 0.0) if cpu_by_pid is not None else 0.0
        if not amount and not share:
            continue
        who = _find_owner(pid, procs, chat_roots, app_roots, program_roots)
        chat = names.get(who[1], who[1]) if who and who[0] == "chat" else None
        if is_code_checker(" ".join(p.argv[:3])):
            key = chat if chat is not None else OTHER_ITEM
            checkers[key] = checkers.get(key, 0) + amount
            checkers_cpu[key] = checkers_cpu.get(key, 0.0) + share
            if who is not None and chat is not None:
                checker_stops.setdefault(key, {"kind": "checkers", "id": who[1]})
        elif who is not None and chat is not None:
            # the chat itself and whatever it is running right now (a test run, a browser)
            chats[chat] = chats.get(chat, 0) + amount
            chats_cpu[chat] = chats_cpu.get(chat, 0.0) + share
            chat_stops.setdefault(
                chat, {"kind": "chat", "id": (chat_ids or {}).get(who[1], who[1])}
            )
        elif who is not None and who[0] == "app":
            app_mem[who[1]] = app_mem.get(who[1], 0) + amount
            app_cpu[who[1]] = app_cpu.get(who[1], 0.0) + share
            app_stops.setdefault(who[1], {"kind": "app", "id": app_programs[who[1]]})
        else:
            key = who[1] if who is not None else unowned_item(p)
            programs_mem[key] = programs_mem.get(key, 0) + amount
            programs_cpu[key] = programs_cpu.get(key, 0.0) + share

    has_cpu = cpu_by_pid is not None
    return [
        _group("chats", "Chats", chats, chats_cpu if has_cpu else None, chat_stops),
        _group(
            "checkers",
            "Code checkers",
            checkers,
            checkers_cpu if has_cpu else None,
            checker_stops,
        ),
        # Everything not a chat, a code checker, or a user-built app: the built-in apps (this
        # one included) and the background services, each an item, and what none of them
        # started under SSH access, Internal system or Other.
        _group(
            "workspace",
            "Workspace",
            programs_mem,
            programs_cpu if has_cpu else None,
            hint="chat app, desktop, built-in apps, background services",
        ),
        _group("apps", "Your apps", app_mem, app_cpu if has_cpu else None, app_stops),
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


def _system_row(
    amount: int, groups: list[dict[str, Any]], cpu_usage: float | None
) -> dict[str, Any]:
    """The rest: what no row holds, and the CPU share the rows together fall short of the whole."""
    cpu = (
        None
        if cpu_usage is None
        else round(max(0.0, cpu_usage - sum(g["cpu"] or 0.0 for g in groups)), 4)
    )
    return _total_row("system", "System", amount, cpu=cpu)


def exact_usage(
    procs: dict[int, Process],
    names: dict[str, str],
    apps: dict[str, str],
    supervised: dict[str, int],
    used: int,
    scratch: dict[str, int] | None = None,
    agents: dict[int, str] | None = None,
    cpu_by_pid: Mapping[int, float] | None = None,
    chat_ids: Mapping[str, str] | None = None,
    program_names: Mapping[str, str] | None = None,
    cpu_usage: float | None = None,
) -> list[dict[str, Any]]:
    """The groups by anonymous memory, then Shared, Temporary files, and System rows, adding up to ``used``.

    Shared is the shared memory segments programs map (Chromium's buffers, the display's),
    counted once: the sum over processes of their proportional share. Scratch space is the files
    in /tmp when it is held in memory (``scratch``, None when it is not). System is the rest of
    the memory in use: what no process holds (the kernel) plus the processes this app cannot
    inspect. It cannot go below zero.
    """
    groups = group_usage(
        procs,
        names,
        apps,
        supervised,
        size=_anonymous,
        agents=agents,
        cpu_by_pid=cpu_by_pid,
        chat_ids=chat_ids,
        program_names=program_names,
    )
    shared = sum(p.shmem for p in procs.values() if p.shmem is not None)
    groups.append(
        _total_row("shared", "Shared", shared, "memory shared between programs")
    )
    if scratch is not None:
        groups.append(_scratch_row(scratch))
    held = sum(g["bytes"] for g in groups)
    groups.append(_system_row(max(0, used - held), groups, cpu_usage))
    return groups


def scaled_usage(
    procs: dict[int, Process],
    names: dict[str, str],
    apps: dict[str, str],
    supervised: dict[str, int],
    used: int,
    scratch: dict[str, int] | None = None,
    agents: dict[int, str] | None = None,
    cpu_by_pid: Mapping[int, float] | None = None,
    chat_ids: Mapping[str, str] | None = None,
    program_names: Mapping[str, str] | None = None,
    cpu_usage: float | None = None,
) -> list[dict[str, Any]]:
    """The groups by resident size, shrunk to the memory in use less the scratch space's, then a
    Temporary files row (when /tmp is held in memory) and a System row for what the programs
    do not account for: the fallback without smaps_rollup."""
    in_scratch = scratch["used"] if scratch is not None else 0
    groups = scale_to_used(
        group_usage(
            procs,
            names,
            apps,
            supervised,
            agents=agents,
            cpu_by_pid=cpu_by_pid,
            chat_ids=chat_ids,
            program_names=program_names,
        ),
        max(0, used - in_scratch),
    )
    if scratch is not None:
        groups.append(_scratch_row(scratch))
    held = sum(g["bytes"] for g in groups)
    groups.append(_system_row(max(0, used - held), groups, cpu_usage))
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
