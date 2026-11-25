#!/usr/bin/env python3
"""What holds this workspace's memory, measured the way the memory guard measures it, and what
could be stopped to free some.

    workspace_memory.py free                  the free memory the guard acts on
    workspace_memory.py list                  everything holding memory, largest first
    workspace_memory.py list --stoppable      only what could be stopped
    workspace_memory.py list --json           the whole report as JSON
    workspace_memory.py show <pid or name>    one process, or one chat, app, service or browser
    workspace_memory.py guard-order           the processes in the order the guard would stop them

The ``free-workspace-memory`` skill is the flow around it: a lead runs ``list --stoppable`` after one of
its agents reports a shed, shows the user the list next to the free memory, and stops only what the
user approves (a scheduled update may go further; see the ``update-self`` skill). This command only
reads: it never stops, kills, or re-tags anything.

Every figure is anonymous memory, what a process made for itself and roughly what stopping it
frees, summed over the processes that belong to a chat, an app, a service or a browser. Which
processes those are is ``oom_priority.processes``' answer, the one the System Monitor app's
breakdown uses, so the two agree. The rest of the report joins four sources, each read
independently so one failing leaves the others intact:

- free memory, read where the memory guard reads it (``oom_priority.meminfo``): the headroom file
  earlyoom runs with while it is fresh, else ``/proc/meminfo``. The output names both the file and
  the field (``MemAvailable``, else ``MemFree``), because under a memory cap the two files disagree
  and only the guard's own figure says when the next heavy command will be shed. It also says how
  much of that is left before the guard acts, at the share of ``MemTotal`` its program line passes
  as ``-m``: the guard stops processes well before free memory reaches zero;
- agents, from ``mngr list --provider local`` rendered through a ``--format`` template: a chat
  (``user_created``) or worker (``agent_created``) can be stopped when its state is ``WAITING``,
  its chat is not busy, and its latest activity is at least ``IDLE_AFTER_SECONDS`` old. A chat is
  busy while a background task will wake its agent, which ``chat_background_tasks`` reads from the
  chats' marker files. The workspace's main chat, named by the chat app's ``main_chat.json``, never
  can: the chat app starts it again as soon as it stops;
- apps, from the shell's ``GET /api/inventory`` joined to the programs supervisord is running: an
  app the workspace is allowed to stop (with a program of its own that no critical app shares, and
  neither critical nor internal itself), with how many windows show it, since that is what decides
  whether stopping it takes something off the user's screen;
- browsers, from the Screen service's ``GET /browsers``: a ``running`` browser can be stopped while
  no desktop window shows the Screen it runs on, per the same inventory document.

Whatever cannot be stopped is listed with why, down to the processes nothing above started, and
the shared memory, the files in ``/tmp`` and the rest of what is in use close the list. A source
that cannot be read leaves what it would have decided unknown (``null`` candidates plus a note),
never "nothing to stop".

``free`` reads nothing but the meminfo files and the memory guard's program file, so it is cheap
enough to poll while memory is short.

Self-contained beyond the stdlib-only ``oom_priority`` and ``chat_background_tasks`` packages
(imported via ``sys.path`` inserts), so it runs under a plain ``python3``.
"""

import argparse
import configparser
import enum
import http.client
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, NamedTuple

# import-path: no-venv -- its only imports beyond the standard library are the stdlib-only `oom_priority` and `chat_background_tasks`
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# import-path: no-venv -- its only imports beyond the standard library are the stdlib-only `oom_priority` and `chat_background_tasks`
sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[3] / "libs" / "chat_background_tasks" / "src"),
)

from chat_background_tasks import markers
from oom_priority import app_registry
from oom_priority.agent_identity import (
    CHAT_LABEL,
    PRIMARY_LABEL,
    WORKER_LABEL,
    is_label_true,
)
from oom_priority.meminfo import (
    HOST_MEMINFO_PATH,
    KIB,
    is_host_meminfo_fresh,
    is_tmpfs,
)
from oom_priority.processes import (
    Owner,
    OwnerKind,
    Process,
    anonymous_bytes,
    find_owner,
    has_breakdown,
    owner_roots,
    read_processes,
    unowned_item,
)
from oom_priority.registry import live_pids_by_agent_id
from oom_priority.supervisor_rpc import supervised_pids

# An agent counts as idle once its turn has ended (``WAITING``) and nothing -- a message to it,
# its own work, a restart -- has happened for this long. Shorter than this, the user is likely
# still reading its reply.
IDLE_AFTER_SECONDS: Final[float] = 15 * 60
IDLE_AGENT_STATE: Final[str] = "WAITING"
# No turn is open, but a background task the agent started is still running.
RUNNING_BACKGROUND_AGENT_STATE: Final[str] = "RUNNING_BACKGROUND"
MID_TURN_AGENT_STATE: Final[str] = "RUNNING"
# Something runs in the agent's window, but mngr has no config for its agent type, so it cannot tell
# whether that is the agent or whether a turn is open.
UNKNOWN_TYPE_AGENT_STATE: Final[str] = "RUNNING_UNKNOWN_AGENT_TYPE"
# Another program holds the agent's window.
REPLACED_AGENT_STATE: Final[str] = "REPLACED"
# mngr could not read the agent's state in time (a host too loaded to answer), so its processes
# may still be alive.
UNOBSERVED_AGENT_STATE: Final[str] = "UNKNOWN"
# The states in which an agent's processes may be alive: a ``STOPPED`` or ``DONE`` agent holds no
# memory.
MEMORY_HOLDING_AGENT_STATES: Final[frozenset[str]] = frozenset(
    {
        MID_TURN_AGENT_STATE,
        UNKNOWN_TYPE_AGENT_STATE,
        REPLACED_AGENT_STATE,
        UNOBSERVED_AGENT_STATE,
        IDLE_AGENT_STATE,
        RUNNING_BACKGROUND_AGENT_STATE,
    }
)
# The label naming the chat an agent belongs to, which the chat app stamps on a chat's agents once it has handed
# off; an agent without it is its own chat.
CHAT_ID_LABEL: Final[str] = "chat_id"
CHAT_KIND: Final[str] = "chat"
# The chat app's record of which chat is the workspace's main chat (its ``DEFAULT_CHAT_DATA_DIR`` and
# ``MAIN_CHAT_FILENAME``), under the main checkout.
MAIN_CHAT_FILE: Final[Path] = Path("data") / ".apps" / "chat" / "main_chat.json"
MAIN_CHAT_ID_KEY: Final[str] = "chat_id"
WORKER_KIND: Final[str] = "worker"
# What a row is called when it is an agent but neither of those: the workspace's own services
# agent, and any other.
SERVICES_AGENT_KIND: Final[str] = "services agent"
AGENT_KIND: Final[str] = "agent"
APP_KIND: Final[str] = "app"
SERVICE_KIND: Final[str] = "service"
BROWSER_KIND: Final[str] = "browser"
# A process no agent, program or browser started.
UNOWNED_KIND: Final[str] = "unowned"

# The fields each listed agent is rendered with, in column order. The display name goes last because
# it is the one free-text value, so a separator inside it cannot shift the other columns.
MNGR_LIST_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "name",
    "state",
    "pid",
    "user_activity_time",
    "agent_activity_time",
    "start_time",
    f"labels.{PRIMARY_LABEL}",
    f"labels.{CHAT_LABEL}",
    f"labels.{WORKER_LABEL}",
    f"labels.{CHAT_ID_LABEL}",
    "labels.display_name",
)
MNGR_LIST_FIELD_SEPARATOR: Final[str] = "|"
# A template rather than ``--format json``/``jsonl``, and scoped to the local provider, as
# system/scripts/collect_bug_report_diagnostics.py lists agents: inside a workspace container the
# template path still answers from local state where the json path has failed outright, and
# without ``--provider local`` mngr probes every cloud provider in the settings, none of which
# can answer from in here. Every agent in the workspace is the local provider's.
MNGR_LIST_ARGV: Final[tuple[str, ...]] = (
    "mngr",
    "list",
    "--provider",
    "local",
    "--on-error",
    "continue",
    "--format",
    MNGR_LIST_FIELD_SEPARATOR.join(f"{{{field}}}" for field in MNGR_LIST_FIELDS),
)
MNGR_LIST_TIMEOUT_SECONDS: Final[float] = 60.0
HTTP_TIMEOUT_SECONDS: Final[float] = 10.0

# The Screen service's address, resolved as its commands (system/apps/screen/src/screen/cli.py) do: the browser
# runs on the Screen and its fleet API lives in the Screen service.
SCREEN_APP_NAME: Final[str] = "screen"
ENV_SCREEN_SERVICE_URL: Final[str] = "MINDS_SCREEN_SERVICE_URL"
DEFAULT_SCREEN_SERVICE_URL: Final[str] = "http://127.0.0.1:8090"
BROWSER_RUNNING_LIFECYCLE: Final[str] = "running"

# The shell's address and its inventory document, as workspace_layout's shell_url and INVENTORY_ROUTE read them.
# The one document carries both every app and every desktop's windows, so one read answers both sections.
ENV_SHELL_URL: Final[str] = "MINDS_WORKSPACE_SERVER_URL"
DEFAULT_SHELL_URL: Final[str] = "http://127.0.0.1:8000"
INVENTORY_ROUTE: Final[str] = "/api/inventory"

MEMINFO_PREFERRED_FIELD: Final[str] = "MemAvailable"
MEMINFO_FALLBACK_FIELD: Final[str] = "MemFree"
MEMINFO_TOTAL_FIELD: Final[str] = "MemTotal"
# The memory guard's supervisord program, whose ``-m <term>[,<kill>]`` is the share of MemTotal, in
# percent, at or below which it starts stopping processes.
EARLYOOM_PROGRAM_CONF: Final[Path] = (
    Path(__file__).resolve().parents[3] / "supervisord.conf.d" / "earlyoom.conf"
)
EARLYOOM_PROGRAM_SECTION: Final[str] = "program:earlyoom"
EARLYOOM_THRESHOLD_FLAG: Final[str] = "-m"
# The names earlyoom's ``--avoid <regex>`` shields, and how many band points lower it scores them
# (OOM_SCORE_AVOID in the fork's kill.c).
EARLYOOM_AVOID_FLAG: Final[str] = "--avoid"
EARLYOOM_AVOID_BAND_POINTS: Final[int] = -300
# earlyoom never picks a process with this ``oom_score_adj``, nor the container's init.
NEVER_KILLED_OOM_SCORE_ADJ: Final[int] = -1000
INIT_PID: Final[int] = 1
GUARD_ORDER_DEFAULT_ROWS: Final[int] = 15
COMMAND_COLUMN_WIDTH: Final[int] = 100
SCRATCH_DIR: Final[Path] = Path("/tmp")
KIB_PER_MIB: Final[int] = 1024
README_SECTION: Final[str] = (
    'system/services/oom_priority/README.md, "Workspace memory"'
)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
_PROC_DIR: Final[Path] = Path("/proc")

RunCommand = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]
FetchJson = Callable[[str, float], Any]
IsChatBusy = Callable[[str], bool]
ReadSupervisedPids = Callable[[], Mapping[str, int]]


ReadScratchKib = Callable[[], "int | None"]


class ListFormat(enum.Enum):
    """What ``list`` prints: everything holding memory, only what could be stopped, or the whole
    report as JSON."""

    EVERYTHING = "everything"
    STOPPABLE = "stoppable"
    JSON = "json"


class CanStop(enum.Enum):
    """Whether a row could be stopped to free memory, as ``list`` prints it. ``ON_SCREEN`` is an
    app a window shows: stopping it takes something off the user's screen, so it is offered last."""

    YES = "yes"
    ON_SCREEN = "on screen"
    NO = "no"
    UNKNOWN = "unknown"


class FreeMemory(NamedTuple):
    """Free memory as the memory guard reads it, naming the file and the field it came from."""

    source: str
    field: str
    free_kib: int
    total_kib: int | None
    # Where the guard starts stopping processes, in percent of MemTotal; None when its program could not be read.
    shed_percent: float | None

    @property
    def shed_line_kib(self) -> int | None:
        if self.shed_percent is None or self.total_kib is None:
            return None
        return int(self.total_kib * self.shed_percent / 100)

    @property
    def room_kib(self) -> int | None:
        """Free memory above the guard's line: what a command can take before the guard stops something."""
        shed_line_kib = self.shed_line_kib
        return None if shed_line_kib is None else max(self.free_kib - shed_line_kib, 0)


class ListedAgent(NamedTuple):
    """One agent as the ``mngr list`` template renders it."""

    agent_id: str
    name: str
    state: str
    pid: int | None
    last_activity: datetime | None
    labels: Mapping[str, str]
    display_name: str | None

    @property
    def chat_id(self) -> str:
        return self.labels.get(CHAT_ID_LABEL) or self.agent_id


class Holding(NamedTuple):
    """The processes that belong to one owner and the memory they made for themselves."""

    # The processes the owner's tree starts at: an agent's main processes, a program's own, a
    # browser's processes that carry its profile.
    root_pids: tuple[int, ...]
    process_pids: tuple[int, ...]
    # None when none of its processes could be read.
    anonymous_kib: int | None


_NOTHING_HELD: Final[Holding] = Holding((), (), None)


class ProcessTable(NamedTuple):
    """One reading of every process and what each owner holds.

    ``unowned`` is what no agent, program or browser started, by the item
    ``oom_priority.processes.unowned_item`` counts it under."""

    processes: Mapping[int, Process]
    holdings: Mapping[Owner, Holding]
    unowned: Mapping[str, Holding]

    def held_by(self, kind: OwnerKind, key: str) -> Holding:
        return self.holdings.get(Owner(kind, key), _NOTHING_HELD)


class AgentCandidate(NamedTuple):
    """An idle chat or worker that stopping would take out of memory."""

    name: str
    agent_id: str
    chat_id: str
    display_name: str | None
    kind: str
    state: str
    last_activity: datetime
    idle_seconds: float
    pids: tuple[int, ...]
    process_pids: tuple[int, ...]
    anonymous_kib: int | None


class WindowIndex(NamedTuple):
    """What the inventory's desktops say: how many windows show each app, and whether one shows the Screen
    (the browser's windows are on it)."""

    count_by_app: Mapping[str, int]
    is_screen_shown: bool


class ShellAnswer(NamedTuple):
    """The shell's inventory document, read and indexed once for every section that needs it, or
    why it could not be. ``windows`` is None when there was no document or it is not shaped as one."""

    url: str
    document: Any | None
    windows: WindowIndex | None
    error: str | None


class AppCandidate(NamedTuple):
    """A running app the workspace is allowed to stop, and how many windows show it.

    ``window_count`` is what a caller weighs: stopping an app no window shows takes nothing off the
    user's screen, while stopping one with windows leaves them on a page that does not answer until
    the app is started again.
    """

    name: str
    display_name: str
    program: str
    window_count: int
    stops_when_no_windows: bool
    pids: tuple[int, ...]
    process_pids: tuple[int, ...]
    anonymous_kib: int | None


class BrowserCandidate(NamedTuple):
    """A running browser while no desktop window shows the Screen."""

    name: str
    controller: str
    tab_count: int
    active_url: str | None
    pids: tuple[int, ...]
    process_pids: tuple[int, ...]
    anonymous_kib: int | None


class HeldEntry(NamedTuple):
    """Something holding memory that is not a candidate, and why: the rest of the answer to "what is
    using the memory"."""

    kind: str
    name: str
    title: str | None
    why: str
    pids: tuple[int, ...]
    process_pids: tuple[int, ...]
    anonymous_kib: int | None
    # Whatever else names it: an agent's id and chat id.
    aliases: tuple[str, ...] = ()


class AgentSection(NamedTuple):
    """The idle agents, or None when the agent list could not be read, plus what went wrong.
    ``others`` is the other agents holding memory, each with why it is not a candidate."""

    candidates: tuple[AgentCandidate, ...] | None
    notes: tuple[str, ...]
    others: tuple[HeldEntry, ...]


class AppSection(NamedTuple):
    """The stoppable running apps, or None when they could not be determined, plus what went wrong.
    ``others`` is the other running programs, each with why it is not a candidate."""

    candidates: tuple[AppCandidate, ...] | None
    notes: tuple[str, ...]
    others: tuple[HeldEntry, ...]


class BrowserSection(NamedTuple):
    """The unviewed browsers, or None when they could not be determined, plus what went wrong.
    ``others`` is the other browsers holding memory, each with why it is not a candidate."""

    candidates: tuple[BrowserCandidate, ...] | None
    notes: tuple[str, ...]
    others: tuple[HeldEntry, ...]


class Totals(NamedTuple):
    """What closes the list so it adds up to the memory in use; each None where it cannot be read.

    ``system_kib`` is what no row holds: the kernel, and the processes this command cannot read."""

    in_use_kib: int | None
    shared_kib: int | None
    temporary_files_kib: int | None
    system_kib: int | None


class Report(NamedTuple):
    """Everything the command found."""

    generated_at: datetime
    free_memory: FreeMemory | None
    agents: AgentSection
    apps: AppSection
    browsers: BrowserSection
    # The processes no agent, program or browser started.
    unowned: tuple[HeldEntry, ...]
    totals: Totals
    processes: Mapping[int, Process]


class Sources(NamedTuple):
    """Where the command reads from; injectable so tests run against a fake ``/proc`` and fake services."""

    proc_dir: Path
    host_meminfo: Path
    earlyoom_program_conf: Path
    run_command: RunCommand
    fetch_json: FetchJson
    read_supervised_pids: ReadSupervisedPids
    read_scratch_kib: ReadScratchKib
    screen_service_url: str
    shell_url: str
    now: datetime
    is_chat_busy: IsChatBusy
    main_chat_file: Path


# Free memory


def _parse_meminfo(path: Path) -> dict[str, int]:
    """The ``Key: <n> kB`` lines of a meminfo file, or empty when it cannot be read."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    values_kib: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            values_kib[key.strip()] = int(fields[0])
    return values_kib


def _earlyoom_words(program_conf: Path) -> list[str] | None:
    """The words of earlyoom's program line, or None when its program file cannot be read."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(program_conf)
        command = parser.get(EARLYOOM_PROGRAM_SECTION, "command")
    except (configparser.Error, OSError):
        return None
    try:
        return shlex.split(command)
    except ValueError:
        return None


def read_shed_percent(program_conf: Path) -> float | None:
    """The share of MemTotal, in percent, at which earlyoom's program line has it start stopping processes."""
    words = _earlyoom_words(program_conf)
    if words is None or EARLYOOM_THRESHOLD_FLAG not in words[:-1]:
        return None
    threshold = words[words.index(EARLYOOM_THRESHOLD_FLAG) + 1]
    try:
        return float(threshold.split(",")[0])
    except ValueError:
        return None


def read_free_memory(
    proc_meminfo: Path, host_meminfo: Path, now: datetime, shed_percent: float | None
) -> FreeMemory | None:
    """``MemAvailable`` (else ``MemFree``) and ``MemTotal`` from the file the memory guard acts on.

    The same preference ``oom_priority.meminfo.read_meminfo`` applies, and for the same reason:
    under a memory cap the container's ``/proc/meminfo`` describes the whole machine or misses what
    the runtime is charged, so earlyoom reads the headroom file instead while it is fresh. A reader
    who takes ``/proc/meminfo`` for the guard's figure can be several gigabytes out, and will keep
    rerunning a command the guard goes on shedding.
    """
    sources = (
        (host_meminfo, proc_meminfo)
        if is_host_meminfo_fresh(host_meminfo, now.timestamp())
        else (proc_meminfo,)
    )
    for source in sources:
        values_kib = _parse_meminfo(source)
        for field in (MEMINFO_PREFERRED_FIELD, MEMINFO_FALLBACK_FIELD):
            if field in values_kib:
                return FreeMemory(
                    source=str(source),
                    field=field,
                    free_kib=values_kib[field],
                    total_kib=values_kib.get(MEMINFO_TOTAL_FIELD),
                    shed_percent=shed_percent,
                )
    return None


# Process memory


def read_process_table(
    proc_dir: Path, registered_agents: Mapping[int, str], supervised: Mapping[str, int]
) -> ProcessTable:
    """Read every process once and give each to its owner (``oom_priority.processes``)."""
    procs = read_processes(proc_dir)
    roots = owner_roots(procs, registered_agents, supervised)
    owners = {pid: find_owner(pid, procs, roots) for pid in procs}
    root_pids: dict[Owner, list[int]] = {}
    for pid, owner in roots.items():
        if pid in procs:
            root_pids.setdefault(owner, []).append(pid)
    owned: dict[Owner, list[int]] = {}
    unowned: dict[str, list[int]] = {}
    for pid, owner in owners.items():
        if owner is not None:
            owned.setdefault(owner, []).append(pid)
        elif procs[pid].rss:
            unowned.setdefault(unowned_item(procs[pid]), []).append(pid)
    return ProcessTable(
        processes=procs,
        holdings={
            owner: _holding(root_pids.get(owner, ()), pids, procs)
            for owner, pids in owned.items()
        },
        unowned={item: _holding((), pids, procs) for item, pids in unowned.items()},
    )


def _holding(
    root_pids: Sequence[int], pids: Sequence[int], procs: Mapping[int, Process]
) -> Holding:
    readings = [
        anonymous
        for anonymous in (anonymous_bytes(procs[pid]) for pid in pids)
        if anonymous is not None
    ]
    return Holding(
        root_pids=tuple(sorted(root_pids)),
        process_pids=tuple(sorted(pids)),
        anonymous_kib=sum(readings) // KIB if readings else None,
    )


# Agents


def parse_listed_agent(line: str) -> ListedAgent | None:
    """One rendered ``mngr list`` line, or None when it is not one (the wrong number of columns, or
    no id or name). mngr renders a null field, and a label the agent does not carry, as empty."""
    columns = line.split(MNGR_LIST_FIELD_SEPARATOR, len(MNGR_LIST_FIELDS) - 1)
    if len(columns) != len(MNGR_LIST_FIELDS):
        return None
    values = dict(zip(MNGR_LIST_FIELDS, (column.strip() for column in columns)))
    if not values["id"] or not values["name"]:
        return None
    activity_times = [
        parsed
        for parsed in (
            _parse_timestamp(values[field])
            for field in ("user_activity_time", "agent_activity_time", "start_time")
        )
        if parsed is not None
    ]
    label_prefix = "labels."
    return ListedAgent(
        agent_id=values["id"],
        name=values["name"],
        state=values["state"],
        pid=int(values["pid"]) if values["pid"].isdigit() else None,
        last_activity=max(activity_times) if activity_times else None,
        labels={
            field.removeprefix(label_prefix): value
            for field, value in values.items()
            if field.startswith(label_prefix) and value
        },
        display_name=values["labels.display_name"] or None,
    )


def run_mngr_list(
    run_command: RunCommand,
) -> tuple[list[ListedAgent] | None, list[str]]:
    """The agents ``mngr list`` rendered, or None when it could not tell, plus notes on what went wrong.

    With ``--on-error continue`` mngr still renders every agent it could list when something fails,
    reports the failure on stderr, and exits non-zero; those agents are kept with the failure as a
    note. A non-zero exit with no agents at all is "unknown", not "no agents", and so is output
    none of whose lines parse as an agent.
    """
    try:
        result = run_command(MNGR_LIST_ARGV, MNGR_LIST_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, [f"could not run `mngr list`: {error}"]
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    agents: list[ListedAgent] = []
    unparsed: list[str] = []
    for line in lines:
        agent = parse_listed_agent(line)
        if agent is None:
            unparsed.append(line)
        else:
            agents.append(agent)
    notes: list[str] = []
    if result.returncode != 0:
        stderr_lines = [
            line.strip() for line in result.stderr.splitlines() if line.strip()
        ]
        notes.append(
            f"mngr list exited {result.returncode}: {'; '.join(stderr_lines) or 'no output'}"
        )
    if unparsed:
        notes.append(
            f"{len(unparsed)} line(s) of `mngr list` output did not parse as an agent, first: {unparsed[0]!r}"
        )
    is_unknown = not agents and (result.returncode != 0 or bool(unparsed))
    return (None if is_unknown else agents), notes


def agent_kind(labels: Mapping[str, str]) -> str | None:
    """``chat`` or ``worker`` by the agent's labels, in the launch wrapper's order; None for the
    primary services agent and for anything carrying neither label (never a candidate)."""
    if is_label_true(labels, PRIMARY_LABEL):
        return None
    if is_label_true(labels, CHAT_LABEL):
        return CHAT_KIND
    if is_label_true(labels, WORKER_LABEL):
        return WORKER_KIND
    return None


def _parse_timestamp(value: str) -> datetime | None:
    """A timestamp as mngr renders a datetime (``2026-09-24 11:00:00.123456+00:00``); None when empty."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def classify_agents(
    agents: Sequence[ListedAgent],
    table: ProcessTable,
    now: datetime,
    is_chat_busy: IsChatBusy,
    main_chat_id: str | None,
) -> tuple[list[AgentCandidate], list[HeldEntry]]:
    """The idle chats and workers, largest memory first, and every other agent holding memory with
    why it is not a candidate. An agent whose chat waits on a background task is not idle: the
    task's completion will start its next turn. The main chat is never a candidate: the chat app
    starts it again as soon as it stops. An agent that is neither a chat nor a worker (the
    workspace's own services agent among them), or that mngr did not list at all, is listed for
    the memory its processes hold."""
    candidates: list[AgentCandidate] = []
    others: list[HeldEntry] = []
    listed_ids: set[str] = set()
    for agent in agents:
        listed_ids.add(agent.agent_id)
        # Its processes: the tree under every pid it registered (codex registers two) and the pid mngr reports.
        holding = table.held_by(OwnerKind.AGENT, agent.agent_id)
        kind = agent_kind(agent.labels)
        if kind is None or agent.state not in MEMORY_HOLDING_AGENT_STATES:
            if holding.process_pids:
                others.append(
                    HeldEntry(
                        kind=kind or _unoffered_agent_kind(agent.labels),
                        name=agent.name,
                        title=agent.display_name,
                        why=_why_agent_is_never_offered(agent, kind),
                        pids=holding.root_pids,
                        process_pids=holding.process_pids,
                        anonymous_kib=holding.anonymous_kib,
                        aliases=(agent.agent_id, agent.chat_id),
                    )
                )
            continue
        idle_seconds = (
            None
            if agent.last_activity is None
            else (now - agent.last_activity).total_seconds()
        )
        why = _why_agent_is_held(agent, idle_seconds, main_chat_id, is_chat_busy)
        if why is not None:
            others.append(
                HeldEntry(
                    kind=kind,
                    name=agent.name,
                    title=agent.display_name,
                    why=why,
                    pids=holding.root_pids,
                    process_pids=holding.process_pids,
                    anonymous_kib=holding.anonymous_kib,
                    aliases=(agent.agent_id, agent.chat_id),
                )
            )
            continue
        assert agent.last_activity is not None and idle_seconds is not None
        candidates.append(
            AgentCandidate(
                name=agent.name,
                agent_id=agent.agent_id,
                chat_id=agent.chat_id,
                display_name=agent.display_name,
                kind=kind,
                state=agent.state,
                last_activity=agent.last_activity,
                idle_seconds=idle_seconds,
                pids=holding.root_pids,
                process_pids=holding.process_pids,
                anonymous_kib=holding.anonymous_kib,
            )
        )
    others += unlisted_agents(
        table, listed_ids, "not in mngr's listing of this workspace's agents"
    )
    return (
        sorted(
            candidates,
            key=lambda c: (*_memory_order(c.anonymous_kib), -c.idle_seconds),
        ),
        _largest_first(others),
    )


def unlisted_agents(
    table: ProcessTable, listed_ids: set[str], why: str
) -> list[HeldEntry]:
    """The agents whose processes hold memory although mngr's listing does not name them."""
    return [
        HeldEntry(
            kind=AGENT_KIND,
            name=owner.key,
            title=None,
            why=why,
            pids=holding.root_pids,
            process_pids=holding.process_pids,
            anonymous_kib=holding.anonymous_kib,
        )
        for owner, holding in table.holdings.items()
        if owner.kind is OwnerKind.AGENT and owner.key not in listed_ids
    ]


def _unoffered_agent_kind(labels: Mapping[str, str]) -> str:
    return SERVICES_AGENT_KIND if is_label_true(labels, PRIMARY_LABEL) else AGENT_KIND


def _why_agent_is_never_offered(agent: ListedAgent, kind: str | None) -> str:
    """Why an agent holding memory is not a candidate whatever it is doing."""
    if kind is not None:
        return f"mngr lists it as {agent.state}, yet its processes are running"
    if is_label_true(agent.labels, PRIMARY_LABEL):
        return "the workspace's own services agent"
    return "neither a chat nor a worker"


def _why_agent_is_held(
    agent: ListedAgent,
    idle_seconds: float | None,
    main_chat_id: str | None,
    is_chat_busy: IsChatBusy,
) -> str | None:
    """Why a chat or worker holding memory is not a candidate, or None when it is one."""
    if main_chat_id is not None and agent.chat_id == main_chat_id:
        return "the main chat: the chat app starts it again as soon as it stops"
    if agent.state == MID_TURN_AGENT_STATE:
        return "mid-turn"
    if agent.state == REPLACED_AGENT_STATE:
        return "another program runs in its window"
    if agent.state == UNKNOWN_TYPE_AGENT_STATE:
        return "running, but of an agent type mngr does not know, so whether it is mid-turn is unknown"
    if agent.state == UNOBSERVED_AGENT_STATE:
        return "its state could not be read, so whether it is mid-turn is unknown"
    if agent.state == RUNNING_BACKGROUND_AGENT_STATE or is_chat_busy(agent.chat_id):
        return "waiting on a background task, which will start its next turn"
    if idle_seconds is None:
        return "no activity time to judge it by"
    if idle_seconds < IDLE_AFTER_SECONDS:
        return f"in use: active {_duration(idle_seconds)} ago"
    return None


def _largest_first(entries: Iterable[HeldEntry]) -> list[HeldEntry]:
    return sorted(entries, key=lambda e: _memory_order(e.anonymous_kib))


def _memory_order(anonymous_kib: int | None) -> tuple[bool, int]:
    """A sort key putting the largest memory first and unknown memory last."""
    return anonymous_kib is None, -(anonymous_kib or 0)


def read_main_chat_id(path: Path) -> tuple[str | None, list[str]]:
    """The chat id the chat app's main-chat file names, or None when there is no main chat, plus a note when
    the file is there but cannot be read, since the main chat may then be listed."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, []
    except (OSError, ValueError) as error:
        return None, [
            f"could not read the main chat from {path}, so it may be listed: {error}"
        ]
    chat_id = raw.get(MAIN_CHAT_ID_KEY) if isinstance(raw, dict) else ""
    if chat_id is None:
        return None, []
    if not isinstance(chat_id, str) or not chat_id:
        return None, [f"{path} does not name a chat, so the main chat may be listed"]
    return chat_id, []


def collect_agent_section(
    sources: Sources,
    agents: Sequence[ListedAgent] | None,
    notes: Sequence[str],
    table: ProcessTable,
) -> AgentSection:
    if agents is None:
        return AgentSection(
            candidates=None,
            notes=tuple(notes),
            others=tuple(
                _largest_first(
                    unlisted_agents(
                        table,
                        set(),
                        "mngr could not list the agents, so whether it is in use is unknown",
                    )
                )
            ),
        )
    main_chat_id, main_chat_notes = read_main_chat_id(sources.main_chat_file)
    candidates, others = classify_agents(
        agents, table, sources.now, sources.is_chat_busy, main_chat_id
    )
    return AgentSection(
        candidates=tuple(candidates),
        notes=(*notes, *main_chat_notes),
        others=tuple(others),
    )


def registered_agent_pids(
    agents: Sequence[ListedAgent] | None, proc_dir: Path
) -> dict[int, str]:
    """pid -> agent id for every agent main process: the live ones the agent-pid registry holds
    and the pid mngr reports for each agent whose processes may be alive."""
    registered = {
        pid: agent_id
        for agent_id, pids in live_pids_by_agent_id(proc_dir).items()
        for pid in pids
    }
    for agent in agents or ():
        if agent.pid is not None and agent.state in MEMORY_HOLDING_AGENT_STATES:
            registered.setdefault(agent.pid, agent.agent_id)
    return registered


# Browsers


def fetch_json_over_http(url: str, timeout_seconds: float) -> Any:
    """GET ``url`` and parse its JSON body. Raises ``OSError`` (a ``URLError`` for an unreachable
    server or an error status), ``http.client.HTTPException``, or ``ValueError`` for a non-JSON body."""
    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
        return json.loads(response.read())


def read_window_index(inventory: Any) -> WindowIndex | None:
    """What the inventory's desktops say about windows, or None when it is not shaped as one
    (which, like an unreachable shell, means "unknown", never "none").
    """
    if not isinstance(inventory, dict) or not isinstance(
        inventory.get("desktops"), list
    ):
        return None
    count_by_app: dict[str, int] = {}
    for desktop in inventory["desktops"]:
        if not isinstance(desktop, dict) or not isinstance(
            desktop.get("windows"), list
        ):
            return None
        for window in desktop["windows"]:
            if not isinstance(window, dict) or not isinstance(window.get("app"), str):
                return None
            app = window["app"]
            count_by_app[app] = count_by_app.get(app, 0) + 1
    return WindowIndex(
        count_by_app=count_by_app,
        is_screen_shown=count_by_app.get(SCREEN_APP_NAME, 0) > 0,
    )


def _browser_controller(browser: Mapping[str, Any]) -> str:
    """Who controls the Screen the browser runs on, in the fleet CLI's ``ls`` words."""
    if browser.get("controller") == "agent":
        return (
            f"agent {browser.get('owner_name') or browser.get('owner_agent_id') or '?'}"
        )
    return "human" if browser.get("controller") == "human" else "free"


def running_browsers(
    browsers: Sequence[Mapping[str, Any]], table: ProcessTable
) -> list[BrowserCandidate]:
    """The running browsers, largest memory first."""
    candidates: list[BrowserCandidate] = []
    for browser in browsers:
        name = browser.get("id")
        if (
            not isinstance(name, str)
            or browser.get("lifecycle") != BROWSER_RUNNING_LIFECYCLE
        ):
            continue
        raw_tabs = browser.get("tabs")
        tabs = (
            [tab for tab in raw_tabs if isinstance(tab, dict)]
            if isinstance(raw_tabs, list)
            else []
        )
        active = next((tab for tab in tabs if tab.get("active")), None)
        active_url = active.get("url") if active is not None else None
        holding = table.held_by(OwnerKind.BROWSER, name)
        candidates.append(
            BrowserCandidate(
                name=name,
                controller=_browser_controller(browser),
                tab_count=len(tabs),
                active_url=active_url
                if isinstance(active_url, str) and active_url
                else None,
                pids=holding.root_pids,
                process_pids=holding.process_pids,
                anonymous_kib=holding.anonymous_kib,
            )
        )
    return sorted(candidates, key=lambda c: _memory_order(c.anonymous_kib))


def held_browsers(
    table: ProcessTable, why: str, except_names: Iterable[str] = ()
) -> list[HeldEntry]:
    """Every browser whose processes hold memory, bar ``except_names``, as not a candidate."""
    skipped = set(except_names)
    return _largest_first(
        HeldEntry(
            kind=BROWSER_KIND,
            name=owner.key,
            title=None,
            why=why,
            pids=holding.root_pids,
            process_pids=holding.process_pids,
            anonymous_kib=holding.anonymous_kib,
        )
        for owner, holding in table.holdings.items()
        if owner.kind is OwnerKind.BROWSER and owner.key not in skipped
    )


def _fetch(fetch_json: FetchJson, url: str) -> tuple[Any, str | None]:
    try:
        return fetch_json(url, HTTP_TIMEOUT_SECONDS), None
    except (OSError, http.client.HTTPException, ValueError) as error:
        return None, str(error)


def read_shell_inventory(sources: Sources) -> ShellAnswer:
    """The shell's inventory document, read and indexed once for the app and browser sections both."""
    url = f"{sources.shell_url}{INVENTORY_ROUTE}"
    document, error = _fetch(sources.fetch_json, url)
    return ShellAnswer(
        url=url,
        document=document,
        windows=None if error is not None else read_window_index(document),
        error=error,
    )


def collect_browser_section(
    sources: Sources, shell: ShellAnswer, table: ProcessTable
) -> BrowserSection:
    fleet_url = f"{sources.screen_service_url}/browsers"
    fleet, fleet_error = _fetch(sources.fetch_json, fleet_url)
    browsers = fleet.get("browsers") if isinstance(fleet, dict) else None
    if fleet_error is not None or not isinstance(browsers, list):
        return BrowserSection(
            None,
            (
                f"could not reach the Screen service at {fleet_url}: {fleet_error}"
                if fleet_error is not None
                else f"the Screen service at {fleet_url} answered without a browsers list",
            ),
            tuple(
                held_browsers(
                    table,
                    "the Screen service could not be read, so whether it may be stopped is unknown",
                )
            ),
        )
    running = running_browsers(
        [browser for browser in browsers if isinstance(browser, dict)], table
    )
    # A browser the service does not list as running is not its to stop, whatever its processes hold.
    unlisted = held_browsers(
        table,
        "the Screen service does not list it as running",
        except_names=[browser.name for browser in running],
    )
    if not running:
        return BrowserSection((), (), tuple(unlisted))

    # Only a running browser holds memory, and only the shell knows whether a window shows the Screen.
    windows = shell.windows
    if windows is None:
        reason = shell.error or "not an inventory document"
        return BrowserSection(
            None,
            (
                f"could not read the shell's windows at {shell.url} ({reason}), so whether a window shows the Screen is unknown",
            ),
            tuple(
                _largest_first(
                    [
                        *unlisted,
                        *held_browsers(
                            table,
                            "whether a window shows the Screen it runs on is unknown",
                            except_names=[browser.name for browser in unlisted],
                        ),
                    ]
                )
            ),
        )
    if not windows.is_screen_shown:
        return BrowserSection(tuple(running), (), tuple(unlisted))
    return BrowserSection(
        (),
        (),
        tuple(
            _largest_first(
                [
                    *unlisted,
                    *(
                        HeldEntry(
                            kind=BROWSER_KIND,
                            name=browser.name,
                            title=browser.active_url,
                            why="a window shows the Screen it runs on",
                            pids=browser.pids,
                            process_pids=browser.process_pids,
                            anonymous_kib=browser.anonymous_kib,
                        )
                        for browser in running
                    ),
                ]
            )
        ),
    )


# Apps


def stoppable_app_candidates(
    apps: Sequence[Mapping[str, Any]],
    windows: WindowIndex,
    pids_by_program: Mapping[str, int],
    table: ProcessTable,
) -> list[AppCandidate]:
    """The running apps the workspace is allowed to stop, largest memory first.

    The filter is the shell's own ``stoppable_program_of``
    (``system/apps/system_interface/imbue/system_interface/shell/data_types.py``), which decides
    what the workspace may stop, park and wake: an app with a program, not critical (the shell, the chat and the terminal are what the
    workspace is reached and talked to through), and not sharing a critical app's program, since
    stopping that program would take the critical app down with it. An internal app the user cannot
    open is left out as well, and so is the Screen: its service holds almost no memory and the
    browser's Chromium processes are the browser's. Only a program supervisord reports running is
    listed: a stopped one frees nothing, and a registry row naming a program supervisord does not
    have at all (a stale row outlives the drop-in that ran it) could not be started again.
    """
    critical_programs = _critical_programs(apps)
    candidates: list[AppCandidate] = []
    for app in apps:
        name = app.get("name")
        program = app.get("program")
        if not isinstance(name, str) or not isinstance(program, str) or not program:
            continue
        if app.get("critical") or app.get("internal") or name == SCREEN_APP_NAME:
            continue
        if program in critical_programs:
            continue
        pid = pids_by_program.get(program)
        if pid is None:
            continue
        holding = table.held_by(OwnerKind.PROGRAM, program)
        candidates.append(
            AppCandidate(
                name=name,
                display_name=_app_title(app, name),
                program=program,
                window_count=windows.count_by_app.get(name, 0),
                stops_when_no_windows=bool(app.get("stop_when_no_windows")),
                pids=(pid,),
                process_pids=holding.process_pids,
                anonymous_kib=holding.anonymous_kib,
            )
        )
    return sorted(candidates, key=lambda c: _memory_order(c.anonymous_kib))


def _app_title(app: Mapping[str, Any], name: str) -> str:
    """The app's display name, else its name."""
    display_name = app.get("display_name")
    return display_name if isinstance(display_name, str) and display_name else name


def _critical_programs(apps: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        app["program"]
        for app in apps
        if app.get("critical")
        and isinstance(app.get("program"), str)
        and app["program"]
    }


def held_programs(
    apps: Sequence[Mapping[str, Any]],
    candidates: Sequence[AppCandidate],
    pids_by_program: Mapping[str, int],
    table: ProcessTable,
) -> list[HeldEntry]:
    """Every running program that is not a candidate's, largest memory first, whether an app the
    workspace may not stop (one row per program, since apps can share one) or one of the
    workspace's own services."""
    critical_programs = _critical_programs(apps)
    screen_programs = {
        app["program"]
        for app in apps
        if app.get("name") == SCREEN_APP_NAME and isinstance(app.get("program"), str)
    }
    candidate_programs = {candidate.program for candidate in candidates}
    titles_by_program: dict[str, list[str]] = {}
    for app in apps:
        name = app.get("name")
        program = app.get("program")
        if not isinstance(name, str) or not isinstance(program, str) or not program:
            continue
        if program in candidate_programs:
            continue
        titles_by_program.setdefault(program, []).append(_app_title(app, name))
    entries: list[HeldEntry] = []
    for program, pid in pids_by_program.items():
        if program in candidate_programs:
            continue
        titles = titles_by_program.get(program)
        holding = table.held_by(OwnerKind.PROGRAM, program)
        entries.append(
            HeldEntry(
                kind=SERVICE_KIND if titles is None else APP_KIND,
                name=program,
                title=None if titles is None else ", ".join(titles),
                why=_why_program_is_held(
                    program, titles is not None, critical_programs, screen_programs
                ),
                pids=(pid,),
                process_pids=holding.process_pids,
                anonymous_kib=holding.anonymous_kib,
            )
        )
    return _largest_first(entries)


def unclassified_programs(
    pids_by_program: Mapping[str, int], table: ProcessTable, why: str
) -> list[HeldEntry]:
    """Every running program, as not a candidate: what is listed when the shell cannot say which
    of them are apps the workspace may stop."""
    return _largest_first(
        HeldEntry(
            kind=SERVICE_KIND,
            name=program,
            title=None,
            why=why,
            pids=(pid,),
            process_pids=table.held_by(OwnerKind.PROGRAM, program).process_pids,
            anonymous_kib=table.held_by(OwnerKind.PROGRAM, program).anonymous_kib,
        )
        for program, pid in pids_by_program.items()
    )


def _why_program_is_held(
    program: str,
    is_app: bool,
    critical_programs: set[str],
    screen_programs: set[str],
) -> str:
    """Why a running program is not a candidate: the most binding reason any app on it carries."""
    if program in critical_programs:
        return "a critical app: the workspace is reached and talked to through it"
    if program in screen_programs:
        return "the Screen's service: its browser is listed on its own"
    if is_app:
        return "an internal app"
    return "a workspace service, not the user's to stop"


def collect_app_section(
    shell: ShellAnswer, pids_by_program: Mapping[str, int], table: ProcessTable
) -> AppSection:
    windows = shell.windows
    apps = shell.document.get("apps") if isinstance(shell.document, dict) else None
    if windows is None or not isinstance(apps, list):
        reason = shell.error or "not an inventory document"
        return AppSection(
            None,
            (
                f"could not read the shell's apps at {shell.url} ({reason}), so which apps run and which windows show them is unknown"
                if windows is None
                else f"the shell at {shell.url} answered without an apps list",
            ),
            tuple(
                unclassified_programs(
                    pids_by_program,
                    table,
                    "the shell could not be read, so whether it is an app the workspace may stop is unknown",
                )
            ),
        )
    if not pids_by_program:
        return AppSection(
            None,
            (
                "could not ask supervisord which programs are running, so which apps hold memory is unknown",
            ),
            (),
        )
    app_records = [app for app in apps if isinstance(app, dict)]
    candidates = stoppable_app_candidates(app_records, windows, pids_by_program, table)
    return AppSection(
        tuple(candidates),
        (),
        tuple(held_programs(app_records, candidates, pids_by_program, table)),
    )


# The report


def collect_report(sources: Sources) -> Report:
    agents, agent_notes = run_mngr_list(sources.run_command)
    pids_by_program = sources.read_supervised_pids()
    table = read_process_table(
        sources.proc_dir,
        registered_agent_pids(agents, sources.proc_dir),
        pids_by_program,
    )
    shell = read_shell_inventory(sources)
    free_memory = read_free_memory(
        sources.proc_dir / "meminfo",
        sources.host_meminfo,
        sources.now,
        read_shed_percent(sources.earlyoom_program_conf),
    )
    return Report(
        generated_at=sources.now,
        free_memory=free_memory,
        agents=collect_agent_section(sources, agents, agent_notes, table),
        apps=collect_app_section(shell, pids_by_program, table),
        browsers=collect_browser_section(sources, shell, table),
        unowned=tuple(
            _largest_first(
                HeldEntry(
                    kind=UNOWNED_KIND,
                    name=item,
                    title=None,
                    why="not started by a chat, an app, a service or a browser",
                    pids=(),
                    process_pids=holding.process_pids,
                    anonymous_kib=holding.anonymous_kib,
                )
                for item, holding in table.unowned.items()
            )
        ),
        totals=read_totals(free_memory, table, sources.read_scratch_kib()),
        processes=table.processes,
    )


def read_totals(
    free_memory: FreeMemory | None, table: ProcessTable, scratch_kib: int | None
) -> Totals:
    """The memory in use and the parts of it no process's own memory accounts for.

    The shared memory segments are counted once (``oom_priority.processes.read_smaps``); what is
    left after every process, those and the files in ``/tmp`` is the system's: the kernel and the
    processes that cannot be read. Without a process whose own memory can be read there is nothing
    to take that remainder from, so it is unknown."""
    in_use_kib = (
        None
        if free_memory is None or free_memory.total_kib is None
        else max(free_memory.total_kib - free_memory.free_kib, 0)
    )
    if not has_breakdown(table.processes):
        return Totals(in_use_kib, None, scratch_kib, None)
    shared_kib = sum(p.shmem or 0 for p in table.processes.values()) // KIB
    held_kib = sum(
        holding.anonymous_kib or 0
        for holding in (*table.holdings.values(), *table.unowned.values())
    )
    return Totals(
        in_use_kib=in_use_kib,
        shared_kib=shared_kib,
        temporary_files_kib=scratch_kib,
        system_kib=None
        if in_use_kib is None
        else max(in_use_kib - held_kib - shared_kib - (scratch_kib or 0), 0),
    )


def read_scratch_kib(path: Path = SCRATCH_DIR) -> int | None:
    """What the files in ``/tmp`` hold of the memory: its used space where it is a tmpfs, None
    where it is on disk or cannot be read."""
    if not is_tmpfs(path):
        return None
    try:
        return shutil.disk_usage(path).used // KIB
    except OSError:
        return None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _held_to_json(entries: Sequence[HeldEntry]) -> list[dict[str, object]]:
    return [
        {
            "kind": entry.kind,
            "name": entry.name,
            "title": entry.title,
            "why": entry.why,
            "pids": list(entry.pids),
            "anonymous_kib": entry.anonymous_kib,
        }
        for entry in entries
    ]


def report_to_json(report: Report) -> dict[str, object]:
    """The report as JSON."""
    free = report.free_memory
    agents = report.agents.candidates
    apps = report.apps.candidates
    browsers = report.browsers.candidates
    return {
        "generated_at": _iso(report.generated_at),
        "idle_after_seconds": IDLE_AFTER_SECONDS,
        "free_memory": None
        if free is None
        else {
            "field": free.field,
            "free_kib": free.free_kib,
            "total_kib": free.total_kib,
            "source": free.source,
            "shed_line_kib": free.shed_line_kib,
            "room_kib": free.room_kib,
        },
        "agents": {
            "candidates": None
            if agents is None
            else [
                {
                    "name": agent.name,
                    "id": agent.agent_id,
                    "display_name": agent.display_name,
                    "kind": agent.kind,
                    "state": agent.state,
                    "last_activity": _iso(agent.last_activity),
                    "idle_seconds": round(agent.idle_seconds),
                    "pids": list(agent.pids),
                    "anonymous_kib": agent.anonymous_kib,
                }
                for agent in agents
            ],
            "notes": list(report.agents.notes),
            "others": _held_to_json(report.agents.others),
        },
        "apps": {
            "candidates": None
            if apps is None
            else [
                {
                    "name": app.name,
                    "display_name": app.display_name,
                    "program": app.program,
                    "window_count": app.window_count,
                    "stop_when_no_windows": app.stops_when_no_windows,
                    "pids": list(app.pids),
                    "anonymous_kib": app.anonymous_kib,
                }
                for app in apps
            ],
            "notes": list(report.apps.notes),
            "others": _held_to_json(report.apps.others),
        },
        "browsers": {
            "candidates": None
            if browsers is None
            else [
                {
                    "name": browser.name,
                    "controller": browser.controller,
                    "tab_count": browser.tab_count,
                    "active_url": browser.active_url,
                    "pids": list(browser.pids),
                    "anonymous_kib": browser.anonymous_kib,
                }
                for browser in browsers
            ],
            "notes": list(report.browsers.notes),
            "others": _held_to_json(report.browsers.others),
        },
        "unowned": _held_to_json(report.unowned),
        "totals": {
            "in_use_kib": report.totals.in_use_kib,
            "shared_kib": report.totals.shared_kib,
            "temporary_files_kib": report.totals.temporary_files_kib,
            "system_kib": report.totals.system_kib,
        },
    }


# Rows


class Row(NamedTuple):
    """One thing holding memory as ``list`` and ``show`` print it, whichever section it came from."""

    kind: str
    name: str
    title: str | None
    anonymous_kib: int | None
    can_stop: CanStop
    detail: str
    process_pids: tuple[int, ...]
    # Whatever else names it: an agent's id and chat id, an app's name.
    aliases: tuple[str, ...]


def report_rows(report: Report) -> list[Row]:
    """Every row of the report, largest memory first."""
    rows: list[Row] = []
    for agent in report.agents.candidates or ():
        rows.append(
            Row(
                kind=agent.kind,
                name=agent.name,
                title=agent.display_name,
                anonymous_kib=agent.anonymous_kib,
                can_stop=CanStop.YES,
                detail=f"idle {_duration(agent.idle_seconds)}",
                process_pids=agent.process_pids,
                aliases=(agent.agent_id, agent.chat_id),
            )
        )
    for app in report.apps.candidates or ():
        rows.append(
            Row(
                kind=APP_KIND,
                name=app.program,
                title=app.display_name,
                anonymous_kib=app.anonymous_kib,
                can_stop=CanStop.YES if app.window_count == 0 else CanStop.ON_SCREEN,
                detail=f"{_windows_show(app.window_count)}; "
                + (
                    "stops by itself once none does"
                    if app.stops_when_no_windows
                    else "keeps running when none does"
                ),
                process_pids=app.process_pids,
                aliases=(app.name,),
            )
        )
    for browser in report.browsers.candidates or ():
        rows.append(
            Row(
                kind=BROWSER_KIND,
                name=browser.name,
                title=browser.active_url,
                anonymous_kib=browser.anonymous_kib,
                can_stop=CanStop.YES,
                detail=f"no window shows it; {_count(browser.tab_count, 'tab')}, {browser.controller}",
                process_pids=browser.process_pids,
                aliases=(),
            )
        )
    sections = (
        (report.agents.others, report.agents.candidates is None),
        (report.apps.others, report.apps.candidates is None),
        (report.browsers.others, report.browsers.candidates is None),
        (report.unowned, False),
    )
    for entries, is_unknown in sections:
        for entry in entries:
            rows.append(
                Row(
                    kind=entry.kind,
                    name=entry.name,
                    title=entry.title,
                    anonymous_kib=entry.anonymous_kib,
                    can_stop=CanStop.UNKNOWN if is_unknown else CanStop.NO,
                    detail=entry.why,
                    process_pids=entry.process_pids,
                    aliases=entry.aliases,
                )
            )
    return sorted(rows, key=lambda row: _memory_order(row.anonymous_kib))


def _windows_show(window_count: int) -> str:
    if window_count == 0:
        return "no window shows it"
    return (
        "1 window shows it" if window_count == 1 else f"{window_count} windows show it"
    )


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _mib(kib: int | None) -> str:
    return "?" if kib is None else f"{kib // KIB_PER_MIB} MiB"


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(cell) for cell in column) for column in zip(header, *rows)]
    return [
        "  " + "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
        for row in (header, *rows)
    ]


def render_free_memory(free: FreeMemory | None) -> str:
    if free is None:
        return "Free memory: unknown (no readable meminfo)"
    total = (
        ""
        if free.total_kib is None
        else f"; {MEMINFO_TOTAL_FIELD} {_mib(free.total_kib)}"
    )
    room = (
        ""
        if free.room_kib is None
        else f"; {_mib(free.room_kib)} of it before the memory guard starts stopping processes "
        f"at {free.shed_percent:g}% of {MEMINFO_TOTAL_FIELD}"
    )
    return f"Free memory: {_mib(free.free_kib)} ({free.field} from {free.source}{total}){room}"


def _row_cells(row: Row) -> tuple[str, ...]:
    return (row.kind, row.name, row.title or "-", _mib(row.anonymous_kib))


def _notes(report: Report) -> list[str]:
    return [
        f"note: {note}"
        for section in (report.agents, report.apps, report.browsers)
        for note in section.notes
    ]


def render_everything(report: Report) -> str:
    """Everything holding memory in one table, largest first, closed by what no process holds."""
    lines = [
        render_free_memory(report.free_memory),
        "",
        "Holding memory, largest first:",
    ]
    rows = report_rows(report)
    totals = report.totals
    closing = [
        ("-", name, "-", _mib(kib), "-", detail)
        for name, kib, detail in (
            ("Shared", totals.shared_kib, "memory programs share, counted once"),
            ("Temporary files", totals.temporary_files_kib, "the files in /tmp"),
            (
                "System",
                totals.system_kib,
                "the rest of what is in use: the kernel, and what cannot be read",
            ),
        )
        if kib is not None
    ]
    lines += _table(
        ("KIND", "NAME", "TITLE", "MEMORY", "CAN STOP", "DETAIL"),
        [(*_row_cells(row), row.can_stop.value, row.detail) for row in rows] + closing,
    )
    if totals.in_use_kib is not None:
        lines.append(f"  In use: {_mib(totals.in_use_kib)}")
    lines += _notes(report)
    lines += [
        "",
        "Memory is each process's own anonymous memory, summed: roughly what stopping it frees.",
        "`list --stoppable` prints only what could be stopped; `show <NAME>` one row's processes.",
        f"Nothing was stopped. Stopping what the user approves: {README_SECTION}.",
    ]
    return "\n".join(lines) + "\n"


def render_stoppable(report: Report) -> str:
    """Only what could be stopped: first what takes nothing off the user's screen, then the apps a
    window shows.

    ``update-self`` runs ``list --stoppable`` from the workspace's own copy of this command, and
    its prose, which comes from a later release, reads what that copy prints. So the arguments keep
    working, and a row keeps its KIND and the NAME a stop takes
    (``.agents/skills/update-self/references/handoff-contract.md``)."""
    lines = [render_free_memory(report.free_memory)]
    rows = report_rows(report)
    groups = (
        (
            "Can be stopped (idle chats and workers, apps no window shows, browsers no window shows):",
            [row for row in rows if row.can_stop is CanStop.YES],
        ),
        (
            "On screen now (apps a window shows; offer these only when the list above is not enough):",
            [row for row in rows if row.can_stop is CanStop.ON_SCREEN],
        ),
    )
    for heading, group in groups:
        lines += ["", heading]
        lines += (
            _table(
                ("KIND", "NAME", "TITLE", "MEMORY", "DETAIL"),
                [(*_row_cells(row), row.detail) for row in group],
            )
            if group
            else ["  none"]
        )
    unknown = [
        label
        for label, candidates in (
            ("chats and workers", report.agents.candidates),
            ("apps", report.apps.candidates),
            ("browsers", report.browsers.candidates),
        )
        if candidates is None
    ]
    lines.append("")
    lines += [
        f"unknown: which {label} could be stopped (see the note below); unknown is not none"
        for label in unknown
    ]
    lines += _notes(report)
    lines += [
        "Memory is each process's own anonymous memory, summed: roughly what stopping it frees.",
        f"Nothing was stopped. Stopping what the user approves: {README_SECTION}.",
    ]
    return "\n".join(lines) + "\n"


# One row, or one process


def _command(process: Process) -> str:
    """A process's command on one line, cut short: an agent's carries its whole prompt."""
    command = " ".join(" ".join(process.argv).split()) or "?"
    if len(command) <= COMMAND_COLUMN_WIDTH:
        return command
    return command[: COMMAND_COLUMN_WIDTH - 3] + "..."


def _process_kib(process: Process) -> int | None:
    anonymous = anonymous_bytes(process)
    return None if anonymous is None else anonymous // KIB


def find_rows(rows: Sequence[Row], target: str) -> list[Row]:
    """The rows ``target`` names: by name, title or alias exactly (case aside), else by a title or
    name that contains it."""
    wanted = target.casefold()
    exact = [
        row
        for row in rows
        if wanted
        in {text.casefold() for text in (row.name, row.title or "", *row.aliases)}
    ]
    if exact:
        return exact
    return [
        row
        for row in rows
        if wanted in row.name.casefold() or wanted in (row.title or "").casefold()
    ]


def render_row(row: Row, processes: Mapping[int, Process]) -> str:
    """One row and each of its processes, largest first."""
    lines = [
        f"{row.kind} {row.name}" + (f" ({row.title})" if row.title else ""),
        f"  Memory: {_mib(row.anonymous_kib)}",
        f"  Can stop: {row.can_stop.value} ({row.detail})",
        "  Processes:",
    ]
    own = sorted(
        (processes[pid] for pid in row.process_pids if pid in processes),
        key=lambda process: _memory_order(_process_kib(process)),
    )
    lines += [
        "  " + line
        for line in _table(
            ("PID", "MEMORY", "COMMAND"),
            [
                (str(process.pid), _mib(_process_kib(process)), _command(process))
                for process in own
            ],
        )
    ]
    return "\n".join(lines) + "\n"


def render_process(process: Process, row: Row | None, oom_score_adj: int | None) -> str:
    """One process: its own memory, the band the guard scores it in, and what it belongs to."""
    belongs = (
        "nothing this command lists"
        if row is None
        else f"{row.kind} {row.name}"
        + (f" ({row.title})" if row.title else "")
        + f"; can stop: {row.can_stop.value} ({row.detail})"
    )
    return (
        "\n".join(
            [
                f"Process {process.pid}: {_command(process)}",
                f"  Memory: {_mib(_process_kib(process))} (its own anonymous memory; resident size {_mib(process.rss // KIB)})",
                f"  Band: {'?' if oom_score_adj is None else oom_score_adj} (oom_score_adj; a higher band is stopped sooner)",
                f"  Belongs to: {belongs}",
            ]
        )
        + "\n"
    )


def render_show(report: Report, target: str, proc_dir: Path) -> tuple[str, bool]:
    """What ``show <target>`` prints, and whether the target was found (and was only one thing)."""
    rows = report_rows(report)
    if target.isdigit():
        pid = int(target)
        process = report.processes.get(pid)
        if process is None:
            return f"No process {pid}.\n", False
        owner = next((row for row in rows if pid in row.process_pids), None)
        return render_process(process, owner, read_oom_score_adj(pid, proc_dir)), True
    matches = find_rows(rows, target)
    if not matches:
        return (
            f"Nothing holding memory is named {target!r}. `list` prints every name.\n",
            False,
        )
    if len(matches) > 1:
        lines = [f"{target!r} names more than one row; ask for one by its NAME:"]
        lines += _table(
            ("KIND", "NAME", "TITLE", "MEMORY"), [_row_cells(row) for row in matches]
        )
        return "\n".join(lines) + "\n", False
    return render_row(matches[0], report.processes), True


# The memory guard's order


class GuardScore(NamedTuple):
    """One process as the memory guard scores it, in KiB."""

    pid: int
    name: str
    oom_score_adj: int
    is_avoided: bool
    counted_kib: int
    score_kib: int


class GuardOrder(NamedTuple):
    """The processes in the order the guard would stop them, or why that cannot be told."""

    scores: tuple[GuardScore, ...]
    # What one band point is worth: (MemTotal + SwapTotal) / 1000.
    band_point_kib: int
    counts_anonymous: bool
    error: str | None


def read_avoid_pattern(program_conf: Path) -> re.Pattern[str] | None:
    """The process names earlyoom's program line shields with ``--avoid``, or None when it has none."""
    words = _earlyoom_words(program_conf)
    if words is None or EARLYOOM_AVOID_FLAG not in words[:-1]:
        return None
    try:
        return re.compile(words[words.index(EARLYOOM_AVOID_FLAG) + 1])
    except re.error:
        return None


def read_oom_score_adj(pid: int, proc_dir: Path) -> int | None:
    try:
        return int((proc_dir / str(pid) / "oom_score_adj").read_text().strip())
    except (OSError, ValueError):
        return None


def _status_kib(status: str) -> dict[str, int]:
    """The ``Key: <n> kB`` lines of a process's status, in KiB by key."""
    values: dict[str, int] = {}
    for line in status.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1] == "kB":
            values[key] = int(fields[0])
    return values


def _truncating_div(numerator: int, denominator: int) -> int:
    """Integer division as C does it, toward zero: the guard's arithmetic."""
    quotient = abs(numerator) // denominator
    return quotient if numerator >= 0 else -quotient


def read_guard_order(
    proc_dir: Path,
    processes: Mapping[int, Process],
    avoid: re.Pattern[str] | None,
) -> GuardOrder:
    """Score every process as the imbue-ai earlyoom fork does (``kill.c``, ``is_larger_badness``):

        memory + VmSwap + VmPTE + oom_score_adj * (MemTotal + SwapTotal) / 1000

    with ``MemTotal`` and ``SwapTotal`` from ``/proc/meminfo``, 300 band points off for a name
    ``--avoid`` matches, and never pid 1 or a process at -1000. The memory is ``VmRSS`` on a Linux
    kernel, which prints ``RssAnon`` in every process's status, and the smaps ``Anonymous`` total
    under gVisor, which prints none. Ties go to the larger memory. A process whose main thread has
    exited while others run is scored by the guard from those threads, which this does not follow.
    """
    meminfo_kib = _parse_meminfo(proc_dir / "meminfo")
    total_kib = meminfo_kib.get(MEMINFO_TOTAL_FIELD)
    if total_kib is None:
        return GuardOrder(
            (),
            0,
            False,
            f"could not read {MEMINFO_TOTAL_FIELD} from {proc_dir / 'meminfo'}",
        )
    band_base_kib = total_kib + meminfo_kib.get("SwapTotal", 0)
    statuses: dict[int, dict[str, int]] = {}
    for pid in processes:
        try:
            status = (proc_dir / str(pid) / "status").read_text(errors="replace")
        except OSError:
            continue
        values = _status_kib(status)
        # A task with no memory of its own (a kernel thread, a zombie) is never the guard's pick.
        if "VmRSS" in values:
            statuses[pid] = values
    counts_anonymous = not any("RssAnon" in values for values in statuses.values())
    scores: list[GuardScore] = []
    for pid, values in statuses.items():
        if pid == INIT_PID:
            continue
        oom_score_adj = read_oom_score_adj(pid, proc_dir)
        if oom_score_adj is None or oom_score_adj == NEVER_KILLED_OOM_SCORE_ADJ:
            continue
        try:
            name = (proc_dir / str(pid) / "comm").read_text(errors="replace").strip()
        except OSError:
            continue
        counted_kib = values["VmRSS"]
        if counts_anonymous and counted_kib > 0:
            anonymous = processes[pid].anonymous
            if anonymous is None:
                continue
            counted_kib = anonymous // KIB
        is_avoided = avoid is not None and avoid.search(name) is not None
        band = oom_score_adj + (EARLYOOM_AVOID_BAND_POINTS if is_avoided else 0)
        scores.append(
            GuardScore(
                pid=pid,
                name=name,
                oom_score_adj=oom_score_adj,
                is_avoided=is_avoided,
                counted_kib=counted_kib,
                score_kib=counted_kib
                + values.get("VmSwap", 0)
                + values.get("VmPTE", 0)
                + _truncating_div(band * band_base_kib, 1000),
            )
        )
    scores.sort(key=lambda score: (-score.score_kib, -score.counted_kib))
    return GuardOrder(
        tuple(scores), _truncating_div(band_base_kib, 1000), counts_anonymous, None
    )


def render_guard_order(
    order: GuardOrder, rows: Sequence[Row], limit: int | None
) -> str:
    if order.error is not None:
        return f"The memory guard's order is unknown: {order.error}\n"
    row_by_pid = {pid: row for row in rows for pid in row.process_pids}
    shown = order.scores if limit is None else order.scores[:limit]
    lines = [
        "The memory guard stops one process at a time, the highest score first:",
    ]
    lines += _table(
        ("PID", "PROCESS", "BELONGS TO", "BAND", "MEMORY", "SCORE"),
        [
            (
                str(score.pid),
                score.name,
                _belongs_to(row_by_pid.get(score.pid)),
                f"{score.oom_score_adj}" + (" (avoided)" if score.is_avoided else ""),
                _mib(score.counted_kib),
                _mib(score.score_kib),
            )
            for score in shown
        ],
    )
    if len(shown) < len(order.scores):
        lines.append(
            f"  ... and {len(order.scores) - len(shown)} more; `guard-order --all` prints every one."
        )
    lines += [
        "",
        "A score is the process's memory, swap and page tables plus its band (oom_score_adj) times",
        f"{order.band_point_kib / KIB_PER_MIB:.1f} MiB, a thousandth of the workspace's memory and swap. MEMORY is "
        + (
            "its own anonymous memory, as the guard counts it here."
            if order.counts_anonymous
            else "its resident size, as the guard counts it here."
        ),
        "The guard scores a name its --avoid shields 300 band points lower. It acts on one process,",
        "not a whole chat or app: the chat it belongs to may go on running without it.",
    ]
    return "\n".join(lines) + "\n"


def _belongs_to(row: Row | None) -> str:
    if row is None:
        return "-"
    return f"{row.kind} {row.title or row.name}"


# Entry point


def _run_command(
    argv: Sequence[str], timeout_seconds: float
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout_seconds, check=False
    )


def resolve_screen_service_url() -> str:
    """The Screen service's base URL: the env override, the app registry's row, then the default."""
    override = os.environ.get(ENV_SCREEN_SERVICE_URL, "")
    if override:
        return override.rstrip("/")
    registry = app_registry.registry_path()
    if not registry.is_absolute():
        registry = _REPO_ROOT / registry
    return (
        app_registry.read_app_url(registry, SCREEN_APP_NAME)
        or DEFAULT_SCREEN_SERVICE_URL
    )


def busy_chat_check(environ: Mapping[str, str]) -> IsChatBusy:
    """Whether a chat has a live background task, from its marker files."""
    root = markers.marker_root(environ, _REPO_ROOT)
    return lambda chat_id: markers.is_busy(root, chat_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="What holds this workspace's memory, measured the way the memory guard "
        "measures it, and what could be stopped to free some. Stops nothing."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "free",
        help="Print only the free memory the memory guard acts on, reading nothing but the "
        "meminfo files and the guard's program file.",
    )
    list_parser = commands.add_parser(
        "list",
        help="Print everything holding memory, largest first, with whether each could be "
        "stopped and why not.",
    )
    list_parser.set_defaults(list_format=ListFormat.EVERYTHING)
    list_format = list_parser.add_mutually_exclusive_group()
    list_format.add_argument(
        "--stoppable",
        action="store_const",
        dest="list_format",
        const=ListFormat.STOPPABLE,
        help="Print only what could be stopped: first what takes nothing off the user's "
        "screen, then the apps a window shows.",
    )
    list_format.add_argument(
        "--json",
        action="store_const",
        dest="list_format",
        const=ListFormat.JSON,
        help="Print the whole report as JSON.",
    )
    show_parser = commands.add_parser(
        "show",
        help="Print one process, or one chat, app, service or browser with its processes.",
    )
    show_parser.add_argument(
        "target",
        help="A pid, or the NAME or TITLE `list` prints (a chat's title, an app's program).",
    )
    guard_parser = commands.add_parser(
        "guard-order",
        help="Print the processes in the order the memory guard would stop them.",
    )
    guard_parser.add_argument(
        "--all",
        action="store_true",
        help=f"Print every process, not only the first {GUARD_ORDER_DEFAULT_ROWS}.",
    )
    return parser


def run(args: argparse.Namespace, now: datetime) -> tuple[str, bool]:
    """What the command prints, and whether it found what it was asked for."""
    if args.command == "free":
        free = read_free_memory(
            _PROC_DIR / "meminfo",
            HOST_MEMINFO_PATH,
            now,
            read_shed_percent(EARLYOOM_PROGRAM_CONF),
        )
        return render_free_memory(free) + "\n", True
    report = _collect_live_report(now)
    if args.command == "show":
        return render_show(report, args.target, _PROC_DIR)
    if args.command == "guard-order":
        order = read_guard_order(
            _PROC_DIR, report.processes, read_avoid_pattern(EARLYOOM_PROGRAM_CONF)
        )
        limit = None if args.all else GUARD_ORDER_DEFAULT_ROWS
        return render_guard_order(order, report_rows(report), limit), True
    match args.list_format:
        case ListFormat.JSON:
            return json.dumps(report_to_json(report), indent=2) + "\n", True
        case ListFormat.STOPPABLE:
            return render_stoppable(report), True
        case ListFormat.EVERYTHING:
            return render_everything(report), True
    raise AssertionError(f"unhandled list format {args.list_format!r}")


def main() -> None:
    text, is_found = run(build_parser().parse_args(), datetime.now(timezone.utc))
    sys.stdout.write(text)
    sys.exit(0 if is_found else 1)


def _collect_live_report(now: datetime) -> Report:
    return collect_report(
        Sources(
            proc_dir=_PROC_DIR,
            host_meminfo=HOST_MEMINFO_PATH,
            earlyoom_program_conf=EARLYOOM_PROGRAM_CONF,
            run_command=_run_command,
            fetch_json=fetch_json_over_http,
            read_supervised_pids=supervised_pids,
            read_scratch_kib=read_scratch_kib,
            screen_service_url=resolve_screen_service_url(),
            shell_url=os.environ.get(ENV_SHELL_URL, DEFAULT_SHELL_URL).rstrip("/"),
            now=now,
            is_chat_busy=busy_chat_check(os.environ),
            main_chat_file=markers.main_checkout(_REPO_ROOT) / MAIN_CHAT_FILE,
        )
    )


if __name__ == "__main__":
    main()
