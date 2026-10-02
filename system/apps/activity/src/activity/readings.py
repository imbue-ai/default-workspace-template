"""Taking every reading the memory tab is built from, each independently: one failing becomes a note on the page
rather than an error, and never reads as "nothing there"."""

from datetime import datetime
from pathlib import Path

import httpx
from pydantic import Field

from activity.chats import ChatInfo
from activity.chats import chat_app_url
from activity.chats import fetch_chats
from activity.errors import ChatAppUnavailableError
from activity.errors import SupervisorUnavailableError
from activity.memory_reading import MemorySources
from activity.memory_reading import closing_point
from activity.memory_reading import read_earlyoom_meminfo
from activity.memory_reading import read_memory
from activity.processes import read_process_table
from activity.summary import RegisteredAgent
from activity.summary import SummaryInputs
from activity.supervised_programs import ReadProcessInfo
from activity.supervised_programs import SupervisedProgram
from activity.supervised_programs import read_supervised_programs
from app_manifest.errors import RegistryReadError
from app_manifest.registry import RegistryRow
from app_manifest.registry import read_registry
from imbue.imbue_common.frozen_model import FrozenModel
from oom_priority.badness import find_earlyoom_argv
from oom_priority.registry import live_pids_by_agent_id
from oom_priority.registry import lookup_agent


class ReadingSources(FrozenModel):
    """Where the readings come from; injectable so tests read fake trees."""

    memory: MemorySources = Field(description="The memory files")
    proc_dir: Path = Field(description="The process table")
    registry_path: Path = Field(description="The app registry, data/.state/apps.toml")


def read_registered_agents(proc_dir: Path) -> list[RegisteredAgent]:
    """Every agent with a live process in the agent-pid registry, with the name it registered under."""
    pids_by_agent_id = live_pids_by_agent_id(is_alive=lambda pid: (proc_dir / str(pid)).is_dir())
    agents: list[RegisteredAgent] = []
    for agent_id, pids in sorted(pids_by_agent_id.items()):
        record = lookup_agent(pids[0]) or {}
        agents.append(
            RegisteredAgent(
                agent_id=agent_id,
                agent_name=str(record.get("agent_name", agent_id)),
                is_worker=bool(record.get("is_worker", False)),
                pids=tuple(sorted(pids)),
            )
        )
    return agents


def read_app_rows(registry_path: Path) -> tuple[list[RegistryRow], str | None]:
    """The app registry's rows, plus a note when it could not be read."""
    try:
        return read_registry(registry_path), None
    except RegistryReadError as e:
        return [], f"The app list could not be read: {e}"


def collect_summary_inputs(
    sources: ReadingSources, client: httpx.Client, read_process_info: ReadProcessInfo, now: datetime
) -> SummaryInputs:
    notes: list[str] = []
    app_rows, registry_note = read_app_rows(sources.registry_path)
    if registry_note is not None:
        notes.append(registry_note)

    programs: tuple[SupervisedProgram, ...] | None = None
    try:
        programs = tuple(read_supervised_programs(read_process_info))
    except SupervisorUnavailableError as e:
        notes.append(
            f"Apps and services could not be listed, so their memory is counted under workspace plumbing: {e}"
        )

    chats: tuple[ChatInfo, ...] | None = None
    try:
        chats = tuple(fetch_chats(client, chat_app_url(app_rows)))
    except ChatAppUnavailableError as e:
        notes.append(f"Chat names could not be read, so agents are listed by their own names: {e}")

    memory = read_memory(sources.memory)
    earlyoom = find_earlyoom_argv(sources.proc_dir) if sources.proc_dir.is_dir() else None
    earlyoom_argv = tuple(earlyoom[1]) if earlyoom is not None else None
    closing = (
        closing_point(memory, read_earlyoom_meminfo(sources.memory), earlyoom_argv) if memory is not None else None
    )
    if memory is None:
        notes.append("No memory reading was available from the host, the cgroup, or /proc/meminfo.")

    return SummaryInputs(
        measured_at=now,
        memory=memory,
        closing=closing,
        earlyoom_argv=earlyoom_argv,
        processes=tuple(read_process_table(sources.proc_dir)),
        programs=programs,
        chats=chats,
        agents=tuple(read_registered_agents(sources.proc_dir)),
        app_rows=tuple(app_rows),
        notes=tuple(notes),
    )
