"""What is using the workspace's memory, grouped into things a user recognizes: chats, apps, background services.

Every process is credited to the nearest ancestor that is a known root: an agent's process (from the agent-pid
registry its launcher writes, for every harness) or a supervised program's process. An agent belongs to the chat
whose ``agent_ids`` hold it (a chat that switched account keeps its earlier agents), else it is a helper (a worker a
chat launched) or another agent the workspace runs; every agent's memory lands on a row either way, so nothing goes
missing when the chat app cannot be asked. Chromium's processes, which the browser launches outside its own process
tree, are credited to the browser by name. Anything else -- the service manager, the tmux server, sshd -- is
workspace plumbing.

The "likely first to close" pick is a prediction by the closer's own scoring model (``oom_priority.badness``,
earlyoom's fork; the kernel's cgroup OOM killer scores the same way, without earlyoom's ``--avoid``), and the page
says it is one.
"""

from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from enum import auto
from typing import Final

from pydantic import Field

from activity.apps import BROWSER_APP_NAME
from activity.apps import is_app_stoppable
from activity.apps import is_quittable_by_shell
from activity.chats import ChatInfo
from activity.history import MemorySample
from activity.history import epoch_seconds
from activity.memory_reading import ClosingPoint
from activity.memory_reading import MemoryCloser
from activity.memory_reading import MemoryReading
from activity.memory_reading import MemorySource
from activity.pressure import PressureStretch
from activity.pressure import latest_pressure
from activity.processes import BROWSER_COMMAND_NAMES
from activity.processes import ProcessReading
from activity.processes import as_badness_sample
from activity.supervised_programs import SupervisedProgram
from app_manifest.registry import RegistryRow
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from oom_priority.badness import parse_avoid_regex
from oom_priority.badness import predict_ranking

# The status steps, as fractions of the closing point.
TIGHT_FROM_CLOSING_FRACTION: Final[float] = 0.83
CRITICAL_FROM_CLOSING_FRACTION: Final[float] = 0.98

PLUMBING_ITEM_ID: Final[str] = "plumbing"
UNKNOWN_PROGRAM_STATE: Final[str] = "UNKNOWN"
RUNNING_AGENT_STATE: Final[str] = "running"
_MAX_ANCESTOR_DEPTH: Final[int] = 64

# Plain-language names for the built-in apps and background services; anything else is shown by its own name.
APP_DESCRIPTIONS: Final[dict[str, str]] = {
    "chat": "Where you talk to your agents",
    "terminal": "Command line",
    "files": "Browse your files",
    "browser": "The browser your agents use",
    "getting-started": "Ideas and templates",
    "activity": "This app",
    "memories": "What your chats remember",
}
# Why each always-on app stays running. ``critical`` is a manifest policy, not always a technical need, so each says
# its own reason.
ALWAYS_ON_REASONS: Final[dict[str, str]] = {
    "chat": "Needed to talk to your agents",
    "terminal": "Kept ready in case something needs fixing",
    "browser": "Running because agents use it",
}
UNKNOWN_ALWAYS_ON_REASON: Final[str] = "Its app asks to stay running"
SERVICE_NAMES: Final[dict[str, tuple[str, str]]] = {
    "system_interface": ("Workspace desktop", "Draws the desktop and its windows"),
    "agent-observer": ("Chat activity tracker", "Notices when chats start, stop or need you"),
    "host-backup": ("Backups", "Saves your workspace so it can be restored"),
    "share-gateway": ("Sharing", "Lets you share apps with other people"),
    "owner-exec": ("Desktop app link", "Carries requests from the Imbue app"),
    "xvfb": ("Hidden screen", "The screen the browser draws on"),
    "earlyoom": ("Memory guard", "Closes things when memory runs out, so the workspace keeps running"),
    "cron": ("Scheduler", "Runs scheduled tasks"),
    "oom-tag-backstop": ("Memory priorities", "Tells the memory guard what to close first"),
}
UNKNOWN_SERVICE_DESCRIPTION: Final[str] = "A background service"
UNKNOWN_APP_DESCRIPTION: Final[str] = "An app"
PLUMBING_NAME: Final[str] = "Workspace plumbing"
PLUMBING_DESCRIPTION: Final[str] = (
    "The service manager, terminal sessions, remote access, and any process nothing above accounts for"
)
HELPER_AGENT_DESCRIPTION: Final[str] = "A helper agent a chat started for a task"
OTHER_AGENT_DESCRIPTION: Final[str] = "An agent the workspace runs that isn't one of your chats"
UNNAMED_CHAT_DESCRIPTION: Final[str] = "An agent running now; its chat's name couldn't be read"


class MemoryStatus(UpperCaseStrEnum):
    """How close the workspace is to closing something."""

    COMFORTABLE = auto()
    TIGHT = auto()
    CRITICAL = auto()


class ItemKind(UpperCaseStrEnum):
    """Which group of the page an item belongs to."""

    CHAT = auto()
    HELPER_AGENT = auto()
    AGENT = auto()
    APP = auto()
    SERVICE = auto()
    PLUMBING = auto()


class RegisteredAgent(FrozenModel):
    """An agent with live processes in the agent-pid registry."""

    agent_id: str = Field(description="The agent's mngr id")
    agent_name: str = Field(description="The agent's mngr name")
    is_worker: bool = Field(description="Whether it is a worker (a helper launched by a chat) rather than a chat")
    pids: tuple[int, ...] = Field(description="Its registered live processes (codex registers two)")


class ProcessView(FrozenModel):
    """One process as the page's details show it."""

    pid: int = Field(description="The process id")
    command_name: str = Field(description="The kernel's short name for it")
    command_line: str = Field(description="Its full command line")
    rss_kib: int = Field(description="Resident memory as earlyoom counts it, in KiB")
    oom_score_adj: int = Field(description="Its shedding priority")


class ActivityItem(FrozenModel):
    """One thing the user recognizes, with the processes credited to it."""

    item_id: str = Field(description="A stable id for the page: kind and name")
    kind: ItemKind = Field(description="Which group it belongs to")
    name: str = Field(description="What the user calls it")
    description: str = Field(description="One plain line on what it is")
    state: str = Field(description="A chat's status from the chat app, a program's supervisord state, or running")
    harness: str | None = Field(description="Which harness runs a chat; None otherwise")
    chat_id: str | None = Field(description="The chat app's id for a chat, so the page can stop or start it")
    last_messaged_at: float | None = Field(description="Epoch seconds of a chat's latest message")
    is_critical: bool = Field(description="Whether the workspace keeps it running (an app the shell never stops)")
    is_on_demand: bool = Field(description="Whether it stops by itself once no window shows it")
    always_on_reason: str | None = Field(
        description="Why an app the page offers no Stop for keeps running; None otherwise"
    )
    app_name: str | None = Field(description="An app's registry name, for stopping it; None otherwise")
    is_stoppable: bool = Field(description="Whether the page offers to stop the app through the desktop's Quit")
    is_restarted_on_open: bool = Field(description="Whether the desktop starts the app again on its next request")
    rss_kib: int = Field(description="The summed memory of its processes, in KiB")
    processes: tuple[ProcessView, ...] = Field(description="Its processes, largest first")


class LikelyFirstToClose(FrozenModel):
    """The process the closer would most likely pick first, and the item it belongs to."""

    item_id: str = Field(description="The item the process is credited to")
    pid: int = Field(description="The process id")
    command_name: str = Field(description="The process's short name")


class MemorySummary(FrozenModel):
    """The workspace's memory headline."""

    limit_bytes: int = Field(description="The most the workspace may use")
    used_bytes: int = Field(description="What it uses now")
    status: MemoryStatus = Field(description="How close it is to closing something")
    source: MemorySource = Field(description="Where the figures came from")
    source_detail: str = Field(description="The files and fields read")
    closes_at_used_bytes: int = Field(description="The memory in use at which something gets closed")
    closer: MemoryCloser = Field(description="Whether earlyoom or the kernel's limit closes it")
    min_available_percent: int | None = Field(description="earlyoom's threshold, when it is the closer")
    closing_detail: str = Field(description="How the closing point was worked out")


class ActivitySummary(FrozenModel):
    """Everything the memory tab shows."""

    measured_at: datetime = Field(description="When the reading was taken")
    memory: MemorySummary | None = Field(description="The headline, or None when no memory source could be read")
    chats: tuple[ActivityItem, ...] = Field(description="Chats, helpers and other agents, every one that runs")
    are_chat_names_known: bool = Field(description="Whether the chat app answered, so chats carry their names")
    apps: tuple[ActivityItem, ...] = Field(description="The desktop's apps")
    services: tuple[ActivityItem, ...] = Field(description="Running background services, plus workspace plumbing")
    are_programs_known: bool = Field(description="Whether supervisord answered, so apps and services carry states")
    likely_first_to_close: LikelyFirstToClose | None = Field(description="The closer's likely next pick, if any")
    notes: tuple[str, ...] = Field(description="What could not be read, in words for the page's details")
    pressure: PressureStretch | None = Field(
        description="The latest stretch memory stayed tight long enough to warn about, while it lasts and for an hour after"
    )
    is_preview: bool = Field(description="Whether this is a preview of a proposed change, which stops nothing")


class SummaryInputs(FrozenModel):
    """Every reading a summary is built from."""

    measured_at: datetime = Field(description="When the readings were taken")
    memory: MemoryReading | None = Field(description="The memory reading, if any source answered")
    closing: ClosingPoint | None = Field(description="Where closing starts, when the memory reading exists")
    earlyoom_argv: tuple[str, ...] | None = Field(description="The running earlyoom's argv, or None")
    processes: tuple[ProcessReading, ...] = Field(description="Every readable process")
    programs: tuple[SupervisedProgram, ...] | None = Field(description="Supervised programs, or None if unreadable")
    chats: tuple[ChatInfo, ...] | None = Field(description="The chat app's chats, or None if it could not be asked")
    agents: tuple[RegisteredAgent, ...] = Field(description="Agents with live registered processes")
    app_rows: tuple[RegistryRow, ...] = Field(description="The app registry's rows")
    notes: tuple[str, ...] = Field(description="What could not be read")
    history: tuple[MemorySample, ...] = Field(description="The recorder's readings, oldest first")
    is_preview: bool = Field(description="Whether this is a preview of a proposed change, which stops nothing")


@pure
def memory_status(used_bytes: int, closes_at_used_bytes: int) -> MemoryStatus:
    fraction = used_bytes / closes_at_used_bytes if closes_at_used_bytes > 0 else 1.0
    if fraction < TIGHT_FROM_CLOSING_FRACTION:
        return MemoryStatus.COMFORTABLE
    if fraction < CRITICAL_FROM_CLOSING_FRACTION:
        return MemoryStatus.TIGHT
    return MemoryStatus.CRITICAL


@pure
def app_for_program(program_name: str, app_rows: Sequence[RegistryRow]) -> RegistryRow | None:
    """The visible app a program belongs to: the app it runs, or the app whose sidecar it is (``terminal-pty``
    belongs to ``terminal``)."""
    visible = [row for row in app_rows if not row.internal and row.display_name is not None]
    for row in visible:
        if row.program == program_name:
            return row
    for row in visible:
        if program_name.startswith(f"{row.name}-"):
            return row
    return None


@pure
def credit_processes(
    processes: Sequence[ProcessReading], item_id_by_root_pid: Mapping[int, str], browser_item_id: str | None
) -> dict[int, str]:
    """Each process's item: its nearest ancestor-or-self root's, Chromium's to the browser, the rest to plumbing."""
    parent_by_pid = {process.pid: process.parent_pid for process in processes}
    item_by_pid: dict[int, str] = {}
    for process in processes:
        current: int | None = process.pid
        item_id: str | None = None
        for _ in range(_MAX_ANCESTOR_DEPTH):
            if current is None or current == 0:
                break
            if current in item_id_by_root_pid:
                item_id = item_id_by_root_pid[current]
                break
            current = parent_by_pid.get(current)
        if item_id is None and browser_item_id is not None and process.command_name in BROWSER_COMMAND_NAMES:
            item_id = browser_item_id
        item_by_pid[process.pid] = item_id or PLUMBING_ITEM_ID
    return item_by_pid


@pure
def likely_first_to_close(
    processes: Sequence[ProcessReading],
    item_by_pid: Mapping[int, str],
    closing: ClosingPoint,
    earlyoom_argv: Sequence[str] | None,
) -> LikelyFirstToClose | None:
    """The closer's top pick: earlyoom's (with its ``--avoid``), or the kernel's when it acts first."""
    avoid_regex = (
        parse_avoid_regex(list(earlyoom_argv))
        if closing.closer is MemoryCloser.MEMORY_GUARD and earlyoom_argv is not None
        else None
    )
    ranking = predict_ranking(
        [as_badness_sample(process) for process in processes],
        total_kib=closing.badness_total_kib,
        avoid_regex=avoid_regex,
        excluded_pids=set(),
    )
    if not ranking:
        return None
    top = ranking[0]
    return LikelyFirstToClose(item_id=item_by_pid[top.pid], pid=top.pid, command_name=top.comm)


@pure
def _process_views(processes: Sequence[ProcessReading]) -> tuple[ProcessView, ...]:
    ordered = sorted(processes, key=lambda process: -process.rss_kib)
    return tuple(
        ProcessView(
            pid=process.pid,
            command_name=process.command_name,
            command_line=process.command_line,
            rss_kib=process.rss_kib,
            oom_score_adj=process.oom_score_adj,
        )
        for process in ordered
    )


@pure
def agent_item_ids(chats: Sequence[ChatInfo] | None, agents: Sequence[RegisteredAgent]) -> dict[str, str]:
    """Each registered agent's item: its chat's when a chat holds it, else its own row."""
    chat_item_by_agent_id: dict[str, str] = {}
    for chat in chats or ():
        for agent_id in chat.agent_ids:
            chat_item_by_agent_id[agent_id] = f"chat:{chat.chat_id}"
    return {agent.agent_id: chat_item_by_agent_id.get(agent.agent_id, f"agent:{agent.agent_id}") for agent in agents}


@pure
def agent_description(agent: RegisteredAgent, are_chat_names_known: bool) -> str:
    """What an agent no chat holds is: a worker is a helper; otherwise, with chat names known, it is something the
    workspace runs (the services agent, a chat's earlier agent), and without them it may well be a chat."""
    if agent.is_worker:
        return HELPER_AGENT_DESCRIPTION
    if are_chat_names_known:
        return OTHER_AGENT_DESCRIPTION
    return UNNAMED_CHAT_DESCRIPTION


@pure
def build_summary(inputs: SummaryInputs) -> ActivitySummary:
    programs = inputs.programs or ()
    program_by_name = {program.name: program for program in programs}
    item_id_by_root_pid: dict[int, str] = {}

    item_by_agent_id = agent_item_ids(inputs.chats, inputs.agents)
    for agent in inputs.agents:
        for pid in agent.pids:
            item_id_by_root_pid[pid] = item_by_agent_id[agent.agent_id]

    for program in programs:
        if program.pid is None:
            continue
        app_row = app_for_program(program.name, inputs.app_rows)
        item_id_by_root_pid[program.pid] = f"app:{app_row.name}" if app_row is not None else f"service:{program.name}"

    visible_apps = [
        row for row in inputs.app_rows if not row.internal and row.display_name is not None and row.program is not None
    ]
    browser_item_id = f"app:{BROWSER_APP_NAME}" if any(row.name == BROWSER_APP_NAME for row in visible_apps) else None
    item_by_pid = credit_processes(inputs.processes, item_id_by_root_pid, browser_item_id)
    processes_by_item: dict[str, list[ProcessReading]] = {}
    for process in inputs.processes:
        processes_by_item.setdefault(item_by_pid[process.pid], []).append(process)

    def item(
        item_id: str,
        kind: ItemKind,
        name: str,
        description: str,
        state: str,
        harness: str | None,
        chat_id: str | None,
        last_messaged_at: float | None,
        is_critical: bool,
        is_on_demand: bool,
        always_on_reason: str | None,
        app_name: str | None,
        is_stoppable: bool,
        is_restarted_on_open: bool,
    ) -> ActivityItem:
        credited = processes_by_item.get(item_id, [])
        return ActivityItem(
            item_id=item_id,
            kind=kind,
            name=name,
            description=description,
            state=state,
            harness=harness,
            chat_id=chat_id,
            last_messaged_at=last_messaged_at,
            is_critical=is_critical,
            is_on_demand=is_on_demand,
            always_on_reason=always_on_reason,
            app_name=app_name,
            is_stoppable=is_stoppable,
            is_restarted_on_open=is_restarted_on_open,
            rss_kib=sum(process.rss_kib for process in credited),
            processes=_process_views(credited),
        )

    chat_items = [
        item(
            item_id=f"chat:{chat.chat_id}",
            kind=ItemKind.CHAT,
            name=chat.title,
            description="",
            state=chat.status,
            harness=chat.harness,
            chat_id=chat.chat_id,
            last_messaged_at=chat.last_messaged_at,
            is_critical=False,
            is_on_demand=False,
            always_on_reason=None,
            app_name=None,
            is_stoppable=False,
            is_restarted_on_open=False,
        )
        for chat in inputs.chats or ()
    ]
    unchatted_agents = [agent for agent in inputs.agents if item_by_agent_id[agent.agent_id].startswith("agent:")]
    agent_items = [
        item(
            item_id=f"agent:{agent.agent_id}",
            kind=ItemKind.HELPER_AGENT if agent.is_worker else ItemKind.AGENT,
            name=agent.agent_name,
            description=agent_description(agent, are_chat_names_known=inputs.chats is not None),
            state=RUNNING_AGENT_STATE,
            harness=None,
            chat_id=None,
            last_messaged_at=None,
            is_critical=False,
            is_on_demand=False,
            always_on_reason=None,
            app_name=None,
            is_stoppable=False,
            is_restarted_on_open=False,
        )
        for agent in unchatted_agents
    ]

    apps = tuple(
        item(
            item_id=f"app:{row.name}",
            kind=ItemKind.APP,
            name=str(row.display_name),
            description=APP_DESCRIPTIONS.get(row.name, UNKNOWN_APP_DESCRIPTION),
            state=program_by_name[row.program].state if row.program in program_by_name else UNKNOWN_PROGRAM_STATE,
            harness=None,
            chat_id=None,
            last_messaged_at=None,
            is_critical=row.critical,
            is_on_demand=row.stop_when_no_windows,
            always_on_reason=ALWAYS_ON_REASONS.get(row.name, UNKNOWN_ALWAYS_ON_REASON)
            if row.critical
            else ALWAYS_ON_REASONS.get(row.name),
            app_name=str(row.name),
            is_stoppable=is_app_stoppable(row, inputs.app_rows),
            is_restarted_on_open=is_quittable_by_shell(row, inputs.app_rows),
        )
        for row in visible_apps
    )

    service_items = [
        item(
            item_id=f"service:{program.name}",
            kind=ItemKind.SERVICE,
            name=SERVICE_NAMES.get(program.name, (program.name, UNKNOWN_SERVICE_DESCRIPTION))[0],
            description=SERVICE_NAMES.get(program.name, (program.name, UNKNOWN_SERVICE_DESCRIPTION))[1],
            state=program.state,
            harness=None,
            chat_id=None,
            last_messaged_at=None,
            is_critical=True,
            is_on_demand=False,
            always_on_reason=None,
            app_name=None,
            is_stoppable=False,
            is_restarted_on_open=False,
        )
        for program in programs
        if program.pid is not None and app_for_program(program.name, inputs.app_rows) is None
    ]
    plumbing = item(
        item_id=PLUMBING_ITEM_ID,
        kind=ItemKind.PLUMBING,
        name=PLUMBING_NAME,
        description=PLUMBING_DESCRIPTION,
        state="RUNNING",
        harness=None,
        chat_id=None,
        last_messaged_at=None,
        is_critical=True,
        is_on_demand=False,
        always_on_reason=None,
        app_name=None,
        is_stoppable=False,
        is_restarted_on_open=False,
    )
    services = tuple(sorted(service_items, key=lambda service: -service.rss_kib)) + (plumbing,)

    memory: MemorySummary | None = None
    first_to_close: LikelyFirstToClose | None = None
    pressure: PressureStretch | None = None
    if inputs.memory is not None and inputs.closing is not None:
        tight_from_kib = int(inputs.closing.used_bytes * TIGHT_FROM_CLOSING_FRACTION) // 1024
        # The live reading is the newest point, so the warning never outlasts the headline by the recorder's minute.
        live = MemorySample(
            at_epoch_seconds=epoch_seconds(inputs.measured_at),
            used_kib=inputs.memory.used_bytes // 1024,
            limit_kib=inputs.memory.limit_bytes // 1024,
        )
        pressure = latest_pressure((*inputs.history, live), tight_from_kib, live.at_epoch_seconds)
        memory = MemorySummary(
            limit_bytes=inputs.memory.limit_bytes,
            used_bytes=inputs.memory.used_bytes,
            status=memory_status(inputs.memory.used_bytes, inputs.closing.used_bytes),
            source=inputs.memory.source,
            source_detail=inputs.memory.source_detail,
            closes_at_used_bytes=inputs.closing.used_bytes,
            closer=inputs.closing.closer,
            min_available_percent=inputs.closing.min_available_percent,
            closing_detail=inputs.closing.detail,
        )
        first_to_close = likely_first_to_close(inputs.processes, item_by_pid, inputs.closing, inputs.earlyoom_argv)

    return ActivitySummary(
        measured_at=inputs.measured_at,
        memory=memory,
        chats=tuple(chat_items + agent_items),
        are_chat_names_known=inputs.chats is not None,
        apps=apps,
        services=services,
        are_programs_known=inputs.programs is not None,
        likely_first_to_close=first_to_close,
        notes=inputs.notes,
        pressure=pressure,
        is_preview=inputs.is_preview,
    )
