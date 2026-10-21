"""The running programs and who they belong to: each process's memory, the chats, the apps the
user built, and the programs supervisord runs."""

import json
import re
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NamedTuple

from oom_priority.meminfo import KIB, PROC, parse_kib_fields
from oom_priority.registry import is_process_alive, live_pids_by_agent_id

MNGR_AGENTS_DIR = Path("/home/user/.mngr/agents")
APPS_REGISTRY = Path("data/.state/apps.toml")
# Command-line fragments of the language servers chats start to check code.
CODE_CHECKER_MARKERS = (
    "langserver",
    "language-server",
    "language_server",
    "gopls",
    "rust-analyzer",
    "clangd",
    "pylsp",
)
_AGENT_DIR = re.compile(r"/\.mngr/agents/(agent-[^/\s]+)")


# Friendly names for the supervised programs that are not apps (an app's name comes from the
# registry). A program named nowhere shows under its program name.
PROGRAM_NAMES = {
    "system_interface": "Desktop",
    "agent-observer": "Agent manager",
    "earlyoom": "Memory guard",
    "oom-tag-backstop": "Memory guard",
    "publish_cgroup_meminfo": "Memory guard",
    "resource_sampler": "System Monitor",
    "host-backup": "Backups",
    "share-gateway": "Sharing",
    "cron": "Scheduled tasks",
    "xvfb": "Virtual display",
    "terminal-pty": "Terminal",
    "owner-exec": "Web access",
    "vm-exec-register": "Web access",
    "env-converge": "Environment setup",
}


class Rollup(NamedTuple):
    """What smaps_rollup reports for one process, in bytes."""

    private: int
    pss: int
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
        # proportional share of shared memory segments.
        self.private = private
        self.pss = pss
        self.anonymous = anonymous
        self.shmem = shmem


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


def read_processes(proc: Path = PROC) -> dict[int, Process]:
    out: dict[int, Process] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
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
        rollup = read_rollup(entry) if rss else None
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


def has_breakdown(procs: dict[int, Process]) -> bool:
    """Whether the kernel reported private and proportional memory (smaps_rollup) for any process."""
    return any(p.pss is not None for p in procs.values())


def is_code_checker(command: str) -> bool:
    return any(marker in command for marker in CODE_CHECKER_MARKERS)


def chat_agent_id(command: str) -> str | None:
    """The agent id a command line names through its ``~/.mngr/agents/agent-...`` path, if any."""
    found = _AGENT_DIR.search(command)
    return found.group(1) if found else None


def _agent_records(agents_dir: Path) -> dict[str, dict[str, Any]]:
    """Agent id -> its record, for every record under the agents directory that can be read."""
    records: dict[str, dict[str, Any]] = {}
    if not agents_dir.is_dir():
        return records
    for data_file in agents_dir.glob("agent-*/data.json"):
        try:
            data = json.loads(data_file.read_text())
        except (OSError, ValueError):
            continue
        # A record of an unexpected shape is skipped, so one bad record cannot fail the page.
        if isinstance(data, dict):
            records[data_file.parent.name] = data
    return records


def _labels(record: dict[str, Any]) -> dict[str, Any]:
    labels = record.get("labels")
    return labels if isinstance(labels, dict) else {}


class Chat(NamedTuple):
    """What the page needs to know about a chat from its agent record."""

    name: str  # the name the user knows the chat by
    chat_id: str  # the id the chat app knows it by: its chat_id label, else the agent id itself


def chats(agents_dir: Path = MNGR_AGENTS_DIR) -> dict[str, Chat]:
    """Agent id -> its chat, from one read of the agent records."""
    found: dict[str, Chat] = {}
    for agent_id, record in _agent_records(agents_dir).items():
        labels = _labels(record)
        name = labels.get("display_name") or record.get("name") or agent_id
        found[agent_id] = Chat(
            str(name).removesuffix(" Agent").removesuffix("-Agent"),
            str(labels.get("chat_id") or agent_id),
        )
    return found


def registry_rows(registry: Path = APPS_REGISTRY) -> list[dict[str, Any]]:
    """The app registry's rows; none when it cannot be read or is not shaped as one.

    A row of an unexpected shape is skipped, so one bad row cannot fail the page.
    """
    try:
        rows = tomllib.loads(registry.read_text()).get("apps", [])
    except (OSError, ValueError):
        return []
    return (
        [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    )


def _display_name(row: dict[str, Any]) -> str:
    return str(row.get("display_name") or row.get("name") or row["program"])


def program_names(
    supervised: Mapping[str, int], registry: Path = APPS_REGISTRY
) -> dict[str, str]:
    """supervisord program -> the name the page shows it under, for every running program.

    ``PROGRAM_NAMES`` names a program first (the shell's own registry row calls it Workspace,
    which is the row these items sit under), then its registry row (an app, whatever its
    priority), and the rest show under their program name.
    """
    registered = {
        str(row["program"]): _display_name(row)
        for row in registry_rows(registry)
        if row.get("program")
    }
    return {
        program: PROGRAM_NAMES.get(program) or registered.get(program) or program
        for program in supervised
    }


def agents_by_pid(
    is_alive: Callable[[int], bool] = is_process_alive,
) -> dict[int, str]:
    """pid -> agent id, for every live agent's main process, whatever its harness.

    Each agent records its own main-process pid at launch in oom_priority's agent-pid registry
    (codex records both its TUI and its app-server). This is how a chat on claude, codex, or pi
    is recognised alike. A registered pid that a later, unrelated process has reused counts as
    that agent's until the next agent launch prunes the registry.
    """
    return {
        pid: agent_id
        for agent_id, pids in live_pids_by_agent_id(is_alive).items()
        for pid in pids
    }


def user_apps(registry: Path = APPS_REGISTRY) -> dict[str, str]:
    """supervisord program -> display name, for the apps the user built."""
    return {
        str(row["program"]): _display_name(row)
        for row in registry_rows(registry)
        if row.get("priority") == "user" and row.get("program")
    }
