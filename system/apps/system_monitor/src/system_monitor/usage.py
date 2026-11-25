"""What is using the memory and the CPU: the rows of the page's breakdown.

Programs share memory: those that load the same files (the Python and Node runtimes, Chromium,
system libraries) map the same pages, and each one's resident size (RSS) counts those pages in
full. Where the kernel reports each program's own memory (``/proc/<pid>/smaps_rollup``, or
``smaps`` under gVisor, which has no rollup; see ``oom_priority.processes.read_smaps``), ``exact_usage``
splits the memory in use without estimating: each group holds its programs' anonymous memory
(what they made for themselves, which is what stopping them frees; the file pages they map are
cache the kernel drops under pressure, and the memory guard's figure of what is in use leaves
them out too), the shared memory segments programs map are counted once in a Shared row, the
files in the /tmp scratch space are a Temporary files row, and what no program holds (the
kernel, programs this app cannot inspect) is the System row.

Where no process's own memory can be read, only RSS is available, and ``scaled_usage``
shrinks every group by one factor until they add up to what is in use. Checked against real
stops, a 540 MB chat freed about 245 MB and 950 MB of code checkers freed about 630 MB, so these
rows are a rough guide.

Each row's CPU is the sum of the sampler's per-process shares over its processes; the System row
takes whatever share the rows together fall short of the whole, so the column adds up.
"""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from oom_priority.processes import (
    OTHER_ITEM,
    Owner,
    OwnerKind,
    Process,
    agent_pids,
    anonymous_bytes,
    find_owner,
    is_code_checker,
    owner_roots,
    unowned_item,
)

SCRATCH_DIR = Path("/tmp")
# The Workspace item for the browsers on the Screen, whose processes the Screen's service starts.
BROWSER_ITEM = "Browser"


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


def checker_pids_of_chat(
    procs: Mapping[int, Process], agents: Mapping[int, str] | None, agent_id: str
) -> list[int]:
    """The code checkers a chat's agent started, by pid."""
    roots = {
        pid: Owner(OwnerKind.AGENT, owner_id)
        for pid, owner_id in agent_pids(procs, agents).items()
    }
    return [
        pid
        for pid, p in procs.items()
        if is_code_checker(" ".join(p.argv[:3]))
        and find_owner(pid, procs, roots) == Owner(OwnerKind.AGENT, agent_id)
    ]


def _workspace_item(
    owner: Owner | None, p: Process, program_names: Mapping[str, str]
) -> str:
    """The Workspace item for a process that is no chat's and no user app's."""
    if owner is None:
        return unowned_item(p)
    if owner.kind is OwnerKind.BROWSER:
        return BROWSER_ITEM
    return program_names.get(owner.key, owner.key)


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

    Who a process belongs to is ``oom_priority.processes``' answer: a chat is every process an
    agent's main process started, the main processes being the registered ones (``agents``, any
    harness) and, failing that, claude found by its command line.
    A process's CPU share includes the children it has reaped, so it lands with the group of
    whatever started it. The workspace's own programs (``program_names``: supervised program ->
    the name shown) are the items of the Workspace row, beside the browsers on the Screen as one
    Browser item; what none of them started is "SSH access", "Internal system" or "Other" by its
    command (``unowned_item``).
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
    roots = owner_roots(procs, agents, supervised)

    for pid, p in procs.items():
        amount = size(p)
        share = cpu_by_pid.get(pid, 0.0) if cpu_by_pid is not None else 0.0
        if not amount and not share:
            continue
        who = find_owner(pid, procs, roots)
        chat = (
            names.get(who.key, who.key)
            if who is not None and who.kind is OwnerKind.AGENT
            else None
        )
        if is_code_checker(" ".join(p.argv[:3])):
            key = chat if chat is not None else OTHER_ITEM
            checkers[key] = checkers.get(key, 0) + amount
            checkers_cpu[key] = checkers_cpu.get(key, 0.0) + share
            if who is not None and chat is not None:
                checker_stops.setdefault(key, {"kind": "checkers", "id": who.key})
        elif who is not None and chat is not None:
            # the chat itself and whatever it is running right now (a test run, a browser)
            chats[chat] = chats.get(chat, 0) + amount
            chats_cpu[chat] = chats_cpu.get(chat, 0.0) + share
            chat_stops.setdefault(
                chat, {"kind": "chat", "id": (chat_ids or {}).get(who.key, who.key)}
            )
        elif who is not None and who.kind is OwnerKind.PROGRAM and who.key in apps:
            app = apps[who.key]
            app_mem[app] = app_mem.get(app, 0) + amount
            app_cpu[app] = app_cpu.get(app, 0.0) + share
            app_stops.setdefault(app, {"kind": "app", "id": who.key})
        else:
            key = _workspace_item(who, p, program_names or {})
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
    counted once: the sum over processes of their proportional share, or under gVisor each
    segment's largest resident size among the processes that map it
    (``oom_priority.processes.read_smaps``).
    Scratch space is the files in /tmp when it is held in memory (``scratch``, None when it is
    not). System is the rest of the memory in use: what no process holds (the kernel) plus the
    processes this app cannot inspect. It cannot go below zero.
    """
    groups = group_usage(
        procs,
        names,
        apps,
        supervised,
        size=lambda p: anonymous_bytes(p) or 0,
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
    do not account for: the fallback where no process's own memory can be read."""
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
