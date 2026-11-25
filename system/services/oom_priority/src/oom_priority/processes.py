"""Every running process with the memory it holds, and who each one belongs to.

One reading for everything that says what is using the memory: the System Monitor app's
breakdown and the workspace memory command (``bin/workspace_memory.py``) both go through it, so
a chat, an app or the browser is the same set of processes, with the same figure, in both.

A process belongs to the nearest of its ancestors that starts an owner's tree: an agent's main
process (a chat or a worker), a program supervisord runs (an app or a service), or a browser on
the Screen, found by the Chromium profile on its command line. The Screen's service starts those
browsers as its children, so a browser's processes are the browser's and not the service's.

Stdlib-only (see ``paths``): the workspace memory command runs under a plain ``python3``.
"""

import enum
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final, NamedTuple

from oom_priority.meminfo import KIB, PROC, parse_kib_fields, read_smaps_memory

# Command-line fragments of the language servers chats start to check code.
CODE_CHECKER_MARKERS: Final[tuple[str, ...]] = (
    "langserver",
    "language-server",
    "language_server",
    "gopls",
    "rust-analyzer",
    "clangd",
    "pylsp",
)
_AGENT_DIR: Final[re.Pattern[str]] = re.compile(r"/\.?mngr/agents/(agent-[^/\s]+)")

# The final component of every Screen browser's Chromium profile dir
# (screen.primitives.BROWSER_PROFILE_DIR_PREFIX), which the rest of names the browser.
BROWSER_PROFILE_DIR_PREFIX: Final[str] = "browser-use-user-data-dir-"
_USER_DATA_DIR_FLAG: Final[str] = "--user-data-dir="

# Where a process no agent, program or browser started is counted, by the name of its command: the
# SSH daemon and its sessions, and the machinery that boots and holds the workspace together
# (tini, the bootstrap and its uv wrapper, supervisord, the services agent's tmux and shell).
OTHER_ITEM: Final[str] = "Other"
SSH_ITEM: Final[str] = "SSH access"
INTERNAL_ITEM: Final[str] = "Internal system"
UNOWNED_ITEMS: Final[Mapping[str, str]] = {
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
_MAX_ANCESTRY: Final[int] = 64


class Rollup(NamedTuple):
    """What smaps_rollup reports for one process, in bytes; under gVisor, what its smaps allows
    (no private or proportional figure: gVisor reports every page as private)."""

    private: int | None
    pss: int | None
    anonymous: int | None
    shmem: int | None


class Process:
    def __init__(
        self,
        pid: int,
        ppid: int,
        rss: int,
        argv: list[str],
        private: int | None = None,
        pss: int | None = None,
        anonymous: int | None = None,
        shmem: int | None = None,
    ) -> None:
        self.pid = pid
        self.ppid = ppid
        self.rss = rss  # bytes, shared pages counted in full
        self.argv = argv
        # From smaps_rollup, in bytes, None where the kernel has none or the process is not
        # ours to read: the memory only this process maps, its proportional share (each shared
        # page divided evenly among the processes that map it), the anonymous memory it made for
        # itself (what stopping it frees: its file pages are cache the kernel can drop), and its
        # proportional share of shared memory segments. Under gVisor, from smaps (``read_smaps``):
        # the anonymous memory and the shared segments only.
        self.private = private
        self.pss = pss
        self.anonymous = anonymous
        self.shmem = shmem


class OwnerKind(enum.Enum):
    """What starts a tree of processes. An owner's key is the agent's id, the supervisord
    program's name, or the browser's name."""

    AGENT = "agent"
    PROGRAM = "program"
    BROWSER = "browser"


class Owner(NamedTuple):
    kind: OwnerKind
    key: str


def read_rollup(entry: Path) -> Rollup | None:
    """The figures from a process's smaps_rollup, or None when it cannot be read."""
    try:
        values = parse_kib_fields((entry / "smaps_rollup").read_text())
    except OSError:
        return None  # no such file on this kernel, the process is gone, or not ours to read
    if "Pss" not in values:
        return None
    private = values.get("Private_Clean", 0) + values.get("Private_Dirty", 0)
    return Rollup(
        private, values["Pss"], values.get("Anonymous"), values.get("Pss_Shmem")
    )


def read_smaps(
    pid: int, proc: Path, counted_segment_kib_out: dict[str, int]
) -> Rollup | None:
    """The figures gVisor allows, from ``/proc/<pid>/smaps`` where there is no smaps_rollup, or None
    when it cannot be read either.

    gVisor reports every page as private (``Pss`` equals ``Rss``), so only the anonymous memory and
    the shared memory segments are read: no private or proportional figure. A segment several
    processes map is counted once, at the largest resident size any of them reports (each one's
    covers only the pages it touched): ``counted_segment_kib_out`` holds the size counted so
    far, and this process's ``shmem`` is how far it raises those, so a sum over processes holds
    each once.
    """
    memory = read_smaps_memory(pid, proc)
    if memory is None:
        return None
    raised_kib = 0
    for key, kib in memory.shared_segment_kib.items():
        counted = counted_segment_kib_out.get(key, 0)
        if kib > counted:
            raised_kib += kib - counted
            counted_segment_kib_out[key] = kib
    return Rollup(
        private=None,
        pss=None,
        anonymous=memory.anonymous_kib * KIB,
        shmem=raised_kib * KIB,
    )


def read_processes(proc: Path = PROC) -> dict[int, Process]:
    """Every process ``proc`` lists, by pid; none on a host without one (macOS)."""
    out: dict[int, Process] = {}
    counted_segment_kib: dict[str, int] = {}
    try:
        listed = list(proc.iterdir())
    except OSError:
        return out
    # In pid order, so a shared segment lands on the same processes on every read.
    entries = sorted(
        (entry for entry in listed if entry.name.isdigit()),
        key=lambda entry: int(entry.name),
    )
    for entry in entries:
        try:
            status = (entry / "status").read_text()
            argv = [a for a in (entry / "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            continue  # gone, or not ours to read
        ppid = rss = 0
        for line in status.splitlines():
            if line.startswith("PPid:"):
                ppid = int(line.split()[1])
            elif line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * KIB
        rollup = (
            (
                read_rollup(entry)
                or read_smaps(int(entry.name), proc, counted_segment_kib)
            )
            if rss
            else None
        )
        out[int(entry.name)] = Process(
            int(entry.name),
            ppid,
            rss,
            [a.decode(errors="replace") for a in argv],
            private=rollup.private if rollup else None,
            pss=rollup.pss if rollup else None,
            anonymous=rollup.anonymous if rollup else None,
            shmem=rollup.shmem if rollup else None,
        )
    return out


def has_breakdown(procs: Mapping[int, Process]) -> bool:
    """Whether any process's own memory could be read: smaps_rollup's private and proportional
    figures, or, under gVisor, the anonymous memory in its smaps."""
    return any(p.pss is not None or p.anonymous is not None for p in procs.values())


def anonymous_bytes(process: Process) -> int | None:
    """The memory a process made for itself, roughly what stopping it frees; None when unreadable.

    A kernel that reports Pss but no Anonymous line (before 4.14) falls back to private memory."""
    return process.anonymous if process.anonymous is not None else process.private


def is_code_checker(command: str) -> bool:
    return any(marker in command for marker in CODE_CHECKER_MARKERS)


def chat_agent_id(command: str) -> str | None:
    """The agent id a command line names through its ``<host dir>/agents/agent-...`` path, if any.

    The host dir is ``~/.mngr`` in a container and ``/mngr`` in a Modal sandbox."""
    found = _AGENT_DIR.search(command)
    return found.group(1) if found else None


def agent_pids(
    procs: Mapping[int, Process], registered: Mapping[int, str] | None
) -> dict[int, str]:
    """pid -> agent id for every agent's main process: the registered ones (any harness) and,
    failing that, claude found by its command line."""
    pids = {
        pid: aid
        for pid, p in procs.items()
        if p.argv
        and Path(p.argv[0]).name == "claude"
        and (aid := chat_agent_id(" ".join(p.argv)))
    }
    pids.update({pid: aid for pid, aid in (registered or {}).items() if pid in procs})
    return pids


def browser_pids(procs: Mapping[int, Process]) -> dict[int, str]:
    """pid -> browser name for every process whose argv carries the Chromium profile of one of the
    Screen's browsers (the whole last path component, so ``browser-1`` never matches ``browser-10``)."""
    pids: dict[int, str] = {}
    for pid, p in procs.items():
        for arg in p.argv:
            name = _browser_of_user_data_dir(arg)
            if name is not None:
                pids[pid] = name
                break
    return pids


def _browser_of_user_data_dir(arg: str) -> str | None:
    """The browser a ``--user-data-dir=<path>`` argument names, or None for any other argument."""
    if not arg.startswith(_USER_DATA_DIR_FLAG):
        return None
    _, slash, last = arg.rstrip("/").rpartition("/")
    if not slash or not last.startswith(BROWSER_PROFILE_DIR_PREFIX):
        return None
    return last.removeprefix(BROWSER_PROFILE_DIR_PREFIX) or None


def owner_roots(
    procs: Mapping[int, Process],
    registered_agents: Mapping[int, str] | None,
    supervised: Mapping[str, int],
) -> dict[int, Owner]:
    """pid -> the owner whose tree starts there: every supervisord program's process, every
    Screen browser's processes, and every agent's main process. Where one pid is several, an agent
    outranks a browser, which outranks a program."""
    roots = {
        pid: Owner(OwnerKind.PROGRAM, program) for program, pid in supervised.items()
    }
    roots.update(
        {
            pid: Owner(OwnerKind.BROWSER, name)
            for pid, name in browser_pids(procs).items()
        }
    )
    roots.update(
        {
            pid: Owner(OwnerKind.AGENT, agent_id)
            for pid, agent_id in agent_pids(procs, registered_agents).items()
        }
    )
    return roots


def find_owner(
    pid: int, procs: Mapping[int, Process], roots: Mapping[int, Owner]
) -> Owner | None:
    """The owner ``pid`` descends from, walking its parents to the nearest root; None when no
    root claims it."""
    for _ in range(_MAX_ANCESTRY):
        if pid not in procs or pid <= 1:
            return None
        owner = roots.get(pid)
        if owner is not None:
            return owner
        pid = procs[pid].ppid
    return None


def unowned_item(p: Process) -> str:
    """Where a process nothing started is counted: by its command, else Other.

    The command is the first word of its first argument (sshd rewrites its own to ``sshd: ...``),
    or the script a Python interpreter runs (supervisord is ``python3 /usr/bin/supervisord``).
    """
    words = p.argv[0].split() if p.argv else []
    command = Path(words[0]).name.rstrip(":") if words else ""
    if command.startswith("python") and len(p.argv) > 1:
        command = Path(p.argv[1]).name
    return UNOWNED_ITEMS.get(command, OTHER_ITEM)
