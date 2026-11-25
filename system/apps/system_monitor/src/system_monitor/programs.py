"""The records of who runs what: the chats, the apps the user built, and the programs supervisord
runs. The processes themselves, and which of these each belongs to, are read through
``oom_priority.processes``."""

import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

from oom_priority.meminfo import PROC
from oom_priority.registry import live_pids_by_agent_id

# mngr's host dir is not the same path on every host (a container keeps it at /home/user/.mngr, a
# Modal sandbox at /mngr), so the agent records are found through the agent environment's own.
DEFAULT_MNGR_HOST_DIR = Path("/home/user/.mngr")
APPS_REGISTRY = Path("data/.state/apps.toml")

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
    "env-converge": "Environment setup",
}


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


def mngr_agents_dir(environ: Mapping[str, str] = os.environ) -> Path:
    """Where mngr keeps the agent records: under ``MNGR_HOST_DIR``, else the container's host dir."""
    host_dir = environ.get("MNGR_HOST_DIR", "")
    return (Path(host_dir) if host_dir else DEFAULT_MNGR_HOST_DIR) / "agents"


def chats(agents_dir: Path | None = None) -> dict[str, Chat]:
    """Agent id -> its chat, from one read of the agent records."""
    found: dict[str, Chat] = {}
    for agent_id, record in _agent_records(agents_dir or mngr_agents_dir()).items():
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


def agents_by_pid(proc: Path = PROC) -> dict[int, str]:
    """pid -> agent id, for every live agent's main process, whatever its harness.

    Each agent records its own main-process pid at launch in oom_priority's agent-pid registry
    (codex records both its TUI and its app-server). This is how a chat on claude, codex, or pi
    is recognised alike. A registered pid counts only while it still names the process that
    registered it, so a pid a later, unrelated process reused is left out.
    """
    return {
        pid: agent_id
        for agent_id, pids in live_pids_by_agent_id(proc).items()
        for pid in pids
    }


def user_apps(registry: Path = APPS_REGISTRY) -> dict[str, str]:
    """supervisord program -> display name, for the apps the user built."""
    return {
        str(row["program"]): _display_name(row)
        for row in registry_rows(registry)
        if row.get("priority") == "user" and row.get("program")
    }
