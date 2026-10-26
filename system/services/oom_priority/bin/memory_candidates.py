#!/usr/bin/env python3
"""List what could be stopped to free memory: idle chats and workers, the apps, and browsers no window shows.

A lead runs this after one of its agents reports a shed (see the README's "Memory candidates"),
shows the user the list next to the free memory, and stops only what the user approves (a
scheduled update may go further; see the ``update-self`` skill). This command only reads: it
never stops, kills, or re-tags anything.

It joins four sources, each read independently so one failing leaves the others intact:

- free memory, read where the memory guard reads it (``oom_priority.meminfo``): the headroom file
  earlyoom runs with while it is fresh, else ``/proc/meminfo``. The output names both the file and
  the field (``MemAvailable``, else ``MemFree``), because under a memory cap the two files disagree
  and only the guard's own figure says when the next heavy command will be shed;
- agents, from ``mngr list --provider local`` rendered through a ``--format`` template: a chat
  (``user_created``) or worker (``agent_created``) whose state is ``WAITING``, whose chat is not
  busy, and whose latest activity is at least ``IDLE_AFTER_SECONDS`` old. A chat is busy while a
  background task will wake its agent, which ``chat_background_tasks`` reads from the chats'
  marker files. Its memory is the summed RSS of its process trees, rooted at the pid mngr
  reports plus every live pid the agent-pid registry holds for it;
- apps, from the shell's ``GET /api/inventory`` joined to the programs supervisord is running: an
  app the workspace is allowed to stop (not critical, not internal, and with a program), with how
  many windows show it, since that is what decides whether stopping it takes something off the
  user's screen. Its memory is the summed RSS of its program's process tree;
- browsers, from the browser service's ``GET /browsers``: a ``running`` browser that no desktop
  window shows, per the same inventory document. Its memory is the summed RSS of every
  Chromium process on its profile.

A source that cannot be read is reported as unknown (``null`` candidates plus a note), never as
"nothing to stop".

Self-contained beyond the stdlib-only ``oom_priority`` and ``chat_background_tasks`` packages
(imported via ``sys.path`` inserts), so it runs under a plain ``python3``.
"""

import argparse
import http.client
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
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
from oom_priority.meminfo import HOST_MEMINFO_MAX_AGE_SECONDS, HOST_MEMINFO_PATH
from oom_priority.proctree import list_descendant_pids
from oom_priority.registry import live_pids_by_agent_id
from oom_priority.supervisor_rpc import supervised_pids

# An agent counts as idle once its turn has ended (``WAITING``) and nothing -- a message to it,
# its own work, a restart -- has happened for this long. Shorter than this, the user is likely
# still reading its reply.
IDLE_AFTER_SECONDS: Final[float] = 15 * 60
IDLE_AGENT_STATE: Final[str] = "WAITING"
# The label naming the chat an agent belongs to, which the chat app stamps on a chat's agents once it has handed
# off; an agent without it is its own chat.
CHAT_ID_LABEL: Final[str] = "chat_id"
CHAT_KIND: Final[str] = "chat"
WORKER_KIND: Final[str] = "worker"

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

# The browser service's address, resolved as its CLI (system/apps/browser/src/browser/fleet.py) does.
BROWSER_APP_NAME: Final[str] = "browser"
ENV_BROWSER_SERVICE_URL: Final[str] = "MINDS_BROWSER_SERVICE_URL"
DEFAULT_BROWSER_SERVICE_URL: Final[str] = "http://127.0.0.1:8081"
BROWSER_RUNNING_LIFECYCLE: Final[str] = "running"
# A browser's viewer page names it by this query parameter (browser.primitives.SESSION_QUERY_KEY).
BROWSER_SESSION_QUERY_KEY: Final[str] = "session"
# The final component of every browser's Chromium profile dir (browser.session._profile_dir).
BROWSER_PROFILE_DIR_PREFIX: Final[str] = "browser-use-user-data-dir-"
USER_DATA_DIR_FLAG: Final[str] = "--user-data-dir="

# The shell's address and its inventory document, as workspace_layout's shell_url and INVENTORY_ROUTE read them.
# The one document carries both every app and every desktop's windows, so one read answers both sections.
ENV_SHELL_URL: Final[str] = "MINDS_WORKSPACE_SERVER_URL"
DEFAULT_SHELL_URL: Final[str] = "http://127.0.0.1:8000"
INVENTORY_ROUTE: Final[str] = "/api/inventory"

MEMINFO_PREFERRED_FIELD: Final[str] = "MemAvailable"
MEMINFO_FALLBACK_FIELD: Final[str] = "MemFree"
MEMINFO_TOTAL_FIELD: Final[str] = "MemTotal"
RSS_STATUS_FIELD: Final[str] = "VmRSS"
KIB_PER_MIB: Final[int] = 1024
README_SECTION: Final[str] = (
    'system/services/oom_priority/README.md, "Memory candidates"'
)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
_PROC_DIR: Final[Path] = Path("/proc")

RunCommand = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]
FetchJson = Callable[[str, float], Any]
IsChatBusy = Callable[[str], bool]
ReadSupervisedPids = Callable[[], Mapping[str, int]]


class FreeMemory(NamedTuple):
    """Free memory as the memory guard reads it, naming the file and the field it came from."""

    source: str
    field: str
    free_kib: int
    total_kib: int | None


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


class AgentCandidate(NamedTuple):
    """An idle chat or worker that stopping would take out of memory."""

    name: str
    agent_id: str
    display_name: str | None
    kind: str
    state: str
    last_activity: datetime
    idle_seconds: float
    pids: tuple[int, ...]
    rss_kib: int | None


class ShellAnswer(NamedTuple):
    """The shell's inventory document, read once for every section that needs it, or why it could not be."""

    url: str
    document: Any | None
    error: str | None


class WindowIndex(NamedTuple):
    """What the inventory's desktops say: how many windows show each app, and which browsers a window shows."""

    count_by_app: Mapping[str, int]
    browser_names: frozenset[str]


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
    rss_kib: int | None


class BrowserCandidate(NamedTuple):
    """A running browser no desktop window shows."""

    name: str
    controller: str
    tab_count: int
    active_url: str | None
    pids: tuple[int, ...]
    rss_kib: int | None


class AgentSection(NamedTuple):
    """The idle agents, or None when the agent list could not be read, plus what went wrong."""

    candidates: tuple[AgentCandidate, ...] | None
    notes: tuple[str, ...]


class AppSection(NamedTuple):
    """The stoppable running apps, or None when they could not be determined, plus what went wrong."""

    candidates: tuple[AppCandidate, ...] | None
    notes: tuple[str, ...]


class BrowserSection(NamedTuple):
    """The unviewed browsers, or None when they could not be determined, plus what went wrong."""

    candidates: tuple[BrowserCandidate, ...] | None
    notes: tuple[str, ...]


class Report(NamedTuple):
    """Everything the command found."""

    generated_at: datetime
    free_memory: FreeMemory | None
    agents: AgentSection
    apps: AppSection
    browsers: BrowserSection


class Sources(NamedTuple):
    """Where the command reads from; injectable so tests run against a fake ``/proc`` and fake services."""

    proc_dir: Path
    host_meminfo: Path
    run_command: RunCommand
    fetch_json: FetchJson
    read_supervised_pids: ReadSupervisedPids
    browser_service_url: str
    shell_url: str
    now: datetime
    is_chat_busy: IsChatBusy


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


def read_free_memory(
    proc_meminfo: Path, host_meminfo: Path, now: datetime
) -> FreeMemory | None:
    """``MemAvailable`` (else ``MemFree``) and ``MemTotal`` from the file the memory guard acts on.

    The same preference ``oom_priority.meminfo.read_meminfo`` applies, and for the same reason:
    under a memory cap the container's ``/proc/meminfo`` describes the whole machine or misses what
    the runtime is charged, so earlyoom reads the headroom file instead while it is fresh. A reader
    who takes ``/proc/meminfo`` for the guard's figure can be several gigabytes out, and will keep
    rerunning a command the guard goes on shedding.
    """
    try:
        is_host_fresh = (
            now.timestamp() - host_meminfo.stat().st_mtime
            <= HOST_MEMINFO_MAX_AGE_SECONDS
        )
    except OSError:
        is_host_fresh = False
    sources = (host_meminfo, proc_meminfo) if is_host_fresh else (proc_meminfo,)
    for source in sources:
        values_kib = _parse_meminfo(source)
        for field in (MEMINFO_PREFERRED_FIELD, MEMINFO_FALLBACK_FIELD):
            if field in values_kib:
                return FreeMemory(
                    source=str(source),
                    field=field,
                    free_kib=values_kib[field],
                    total_kib=values_kib.get(MEMINFO_TOTAL_FIELD),
                )
    return None


# Process memory


def read_rss_kib(pid: int, proc_dir: Path) -> int | None:
    """A process's resident set size from ``/proc/<pid>/status``, or None when it is gone or unreadable."""
    try:
        text = (proc_dir / str(pid) / "status").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if key == RSS_STATUS_FIELD and fields and fields[0].isdigit():
            return int(fields[0])
    return None


def process_trees_rss_kib(root_pids: Iterable[int], proc_dir: Path) -> int | None:
    """Summed RSS of the roots and all their descendants, each process counted once.

    RSS counts shared pages in every process that maps them, so the sum overstates what stopping
    them frees; it is a ranking, not a promise. None when no process in the trees could be read.
    """
    pids: set[int] = set()
    for root in root_pids:
        pids.add(root)
        pids.update(list_descendant_pids(root, proc_dir=proc_dir))
    readings = [
        rss
        for rss in (read_rss_kib(pid, proc_dir) for pid in sorted(pids))
        if rss is not None
    ]
    return sum(readings) if readings else None


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


def idle_agent_candidates(
    agents: Sequence[ListedAgent],
    registry_pids: Mapping[str, Sequence[int]],
    proc_dir: Path,
    now: datetime,
    is_chat_busy: IsChatBusy,
) -> list[AgentCandidate]:
    """The idle chats and workers, largest memory first. An agent whose chat waits on a background task is not idle:
    the task's completion will start its next turn."""
    candidates: list[AgentCandidate] = []
    for agent in agents:
        kind = agent_kind(agent.labels)
        if (
            kind is None
            or agent.state != IDLE_AGENT_STATE
            or agent.last_activity is None
        ):
            continue
        idle_seconds = (now - agent.last_activity).total_seconds()
        if idle_seconds < IDLE_AFTER_SECONDS or is_chat_busy(agent.chat_id):
            continue

        # The pid mngr reports plus every pid the agent registered (codex registers two).
        root_pids = set(registry_pids.get(agent.agent_id, ()))
        if agent.pid is not None:
            root_pids.add(agent.pid)
        candidates.append(
            AgentCandidate(
                name=agent.name,
                agent_id=agent.agent_id,
                display_name=agent.display_name,
                kind=kind,
                state=agent.state,
                last_activity=agent.last_activity,
                idle_seconds=idle_seconds,
                pids=tuple(sorted(root_pids)),
                rss_kib=process_trees_rss_kib(root_pids, proc_dir)
                if root_pids
                else None,
            )
        )
    return sorted(
        candidates,
        key=lambda c: (c.rss_kib is None, -(c.rss_kib or 0), -c.idle_seconds),
    )


def collect_agent_section(sources: Sources) -> AgentSection:
    agents, notes = run_mngr_list(sources.run_command)
    if agents is None:
        return AgentSection(candidates=None, notes=tuple(notes))
    registry_pids = live_pids_by_agent_id(
        is_alive=lambda pid: (sources.proc_dir / str(pid)).is_dir()
    )
    candidates = idle_agent_candidates(
        agents, registry_pids, sources.proc_dir, sources.now, sources.is_chat_busy
    )
    return AgentSection(candidates=tuple(candidates), notes=tuple(notes))


# Browsers


def fetch_json_over_http(url: str, timeout_seconds: float) -> Any:
    """GET ``url`` and parse its JSON body. Raises ``OSError`` (a ``URLError`` for an unreachable
    server or an error status), ``http.client.HTTPException``, or ``ValueError`` for a non-JSON body."""
    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
        return json.loads(response.read())


def read_window_index(inventory: Any) -> WindowIndex | None:
    """What the inventory's desktops say about windows, or None when it is not shaped as one
    (which, like an unreachable shell, means "unknown", never "none").

    The browser part mirrors ``workspace_layout.windows.read_app_window_paths``: a window's own
    path and each client's path of it all count.
    """
    if not isinstance(inventory, dict) or not isinstance(
        inventory.get("desktops"), list
    ):
        return None
    count_by_app: dict[str, int] = {}
    browser_names: set[str] = set()
    for desktop in inventory["desktops"]:
        if not isinstance(desktop, dict) or not isinstance(
            desktop.get("windows"), list
        ):
            return None
        for window in desktop["windows"]:
            if not isinstance(window, dict):
                return None
            window_path = window.get("path")
            client_paths = window.get("client_paths", {})
            app = window.get("app")
            if (
                not isinstance(app, str)
                or not isinstance(window_path, str)
                or not isinstance(client_paths, dict)
                or not all(
                    isinstance(client_path, str)
                    for client_path in client_paths.values()
                )
            ):
                return None
            count_by_app[app] = count_by_app.get(app, 0) + 1
            if app != BROWSER_APP_NAME:
                continue
            for path in (window_path, *client_paths.values()):
                values = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get(
                    BROWSER_SESSION_QUERY_KEY
                )
                if values:
                    browser_names.add(values[0])
    return WindowIndex(
        count_by_app=count_by_app, browser_names=frozenset(browser_names)
    )


def chromium_pids_for_browser(browser_name: str, proc_dir: Path) -> tuple[int, ...]:
    """Every process whose argv carries this browser's ``--user-data-dir`` (the whole token, so
    ``browser-1`` never matches ``browser-10``)."""
    profile_suffix = f"/{BROWSER_PROFILE_DIR_PREFIX}{browser_name}"
    try:
        entries = list(proc_dir.iterdir())
    except OSError:
        return ()
    pids: list[int] = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue
        if any(
            arg.startswith(USER_DATA_DIR_FLAG)
            and arg.rstrip("/").endswith(profile_suffix)
            for arg in argv
        ):
            pids.append(int(entry.name))
    return tuple(sorted(pids))


def _browser_controller(browser: Mapping[str, Any]) -> str:
    """Who controls the browser, in the fleet CLI's ``ls`` words."""
    if browser.get("controller") == "agent":
        return (
            f"agent {browser.get('owner_name') or browser.get('owner_agent_id') or '?'}"
        )
    return "human" if browser.get("human_pinned") else "free"


def unviewed_browser_candidates(
    browsers: Sequence[Mapping[str, Any]], shown_names: set[str], proc_dir: Path
) -> list[BrowserCandidate]:
    """The running browsers no window shows, largest memory first."""
    candidates: list[BrowserCandidate] = []
    for browser in browsers:
        name = browser.get("id")
        if (
            not isinstance(name, str)
            or browser.get("lifecycle") != BROWSER_RUNNING_LIFECYCLE
        ):
            continue
        if name in shown_names:
            continue
        raw_tabs = browser.get("tabs")
        tabs = (
            [tab for tab in raw_tabs if isinstance(tab, dict)]
            if isinstance(raw_tabs, list)
            else []
        )
        active = next((tab for tab in tabs if tab.get("active")), None)
        active_url = active.get("url") if active is not None else None
        pids = chromium_pids_for_browser(name, proc_dir)
        candidates.append(
            BrowserCandidate(
                name=name,
                controller=_browser_controller(browser),
                tab_count=len(tabs),
                active_url=active_url
                if isinstance(active_url, str) and active_url
                else None,
                pids=pids,
                rss_kib=process_trees_rss_kib(pids, proc_dir) if pids else None,
            )
        )
    return sorted(candidates, key=lambda c: (c.rss_kib is None, -(c.rss_kib or 0)))


def _fetch(fetch_json: FetchJson, url: str) -> tuple[Any, str | None]:
    try:
        return fetch_json(url, HTTP_TIMEOUT_SECONDS), None
    except (OSError, http.client.HTTPException, ValueError) as error:
        return None, str(error)


def read_shell_inventory(sources: Sources) -> ShellAnswer:
    """The shell's inventory document, read once for the app and browser sections both."""
    url = f"{sources.shell_url}{INVENTORY_ROUTE}"
    document, error = _fetch(sources.fetch_json, url)
    return ShellAnswer(url=url, document=document, error=error)


def collect_browser_section(sources: Sources, shell: ShellAnswer) -> BrowserSection:
    fleet_url = f"{sources.browser_service_url}/browsers"
    fleet, fleet_error = _fetch(sources.fetch_json, fleet_url)
    if fleet_error is not None:
        return BrowserSection(
            None,
            (f"could not reach the browser service at {fleet_url}: {fleet_error}",),
        )
    browsers = fleet.get("browsers") if isinstance(fleet, dict) else None
    if not isinstance(browsers, list):
        return BrowserSection(
            None,
            (f"the browser service at {fleet_url} answered without a browsers list",),
        )
    browser_records = [browser for browser in browsers if isinstance(browser, dict)]
    if not any(
        browser.get("lifecycle") == BROWSER_RUNNING_LIFECYCLE
        for browser in browser_records
    ):
        return BrowserSection((), ())

    # Only a running browser holds memory, and only the shell knows which ones a window shows.
    windows = read_window_index(shell.document) if shell.error is None else None
    if windows is None:
        reason = shell.error or "not an inventory document"
        return BrowserSection(
            None,
            (
                f"could not read the shell's windows at {shell.url} ({reason}), so which browsers no window shows is unknown",
            ),
        )
    return BrowserSection(
        tuple(
            unviewed_browser_candidates(
                browser_records, set(windows.browser_names), sources.proc_dir
            )
        ),
        (),
    )


# Apps


def stoppable_app_candidates(
    apps: Sequence[Mapping[str, Any]],
    windows: WindowIndex,
    pids_by_program: Mapping[str, int],
    proc_dir: Path,
) -> list[AppCandidate]:
    """The running apps the workspace is allowed to stop, largest memory first.

    A critical app is never one (the shell, the chat and the terminal are what the workspace is
    reached and talked to through), nor is an internal app the user cannot open, nor one with no
    program to stop and start. The browser is left out too: its service holds almost no memory and
    its Chromium processes have their own section. Only a program supervisord reports running is
    listed, because a stopped one frees nothing.
    """
    candidates: list[AppCandidate] = []
    for app in apps:
        name = app.get("name")
        program = app.get("program")
        if not isinstance(name, str) or not isinstance(program, str) or not program:
            continue
        if app.get("critical") or app.get("internal") or name == BROWSER_APP_NAME:
            continue
        pid = pids_by_program.get(program)
        if pid is None:
            continue
        display_name = app.get("display_name")
        candidates.append(
            AppCandidate(
                name=name,
                display_name=display_name
                if isinstance(display_name, str) and display_name
                else name,
                program=program,
                window_count=windows.count_by_app.get(name, 0),
                stops_when_no_windows=bool(app.get("stop_when_no_windows")),
                pids=(pid,),
                rss_kib=process_trees_rss_kib((pid,), proc_dir),
            )
        )
    return sorted(candidates, key=lambda c: (c.rss_kib is None, -(c.rss_kib or 0)))


def collect_app_section(sources: Sources, shell: ShellAnswer) -> AppSection:
    windows = read_window_index(shell.document) if shell.error is None else None
    if windows is None:
        reason = shell.error or "not an inventory document"
        return AppSection(
            None,
            (
                f"could not read the shell's apps at {shell.url} ({reason}), so which apps run and which windows show them is unknown",
            ),
        )
    apps = shell.document.get("apps") if isinstance(shell.document, dict) else None
    if not isinstance(apps, list):
        return AppSection(
            None, (f"the shell at {shell.url} answered without an apps list",)
        )
    pids_by_program = sources.read_supervised_pids()
    if not pids_by_program:
        return AppSection(
            None,
            (
                "could not ask supervisord which programs are running, so which apps hold memory is unknown",
            ),
        )
    return AppSection(
        tuple(
            stoppable_app_candidates(
                [app for app in apps if isinstance(app, dict)],
                windows,
                pids_by_program,
                sources.proc_dir,
            )
        ),
        (),
    )


# The report


def collect_report(sources: Sources) -> Report:
    shell = read_shell_inventory(sources)
    return Report(
        generated_at=sources.now,
        free_memory=read_free_memory(
            sources.proc_dir / "meminfo", sources.host_meminfo, sources.now
        ),
        agents=collect_agent_section(sources),
        apps=collect_app_section(sources, shell),
        browsers=collect_browser_section(sources, shell),
    )


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def report_to_json(report: Report) -> dict[str, object]:
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
                    "rss_kib": agent.rss_kib,
                }
                for agent in agents
            ],
            "notes": list(report.agents.notes),
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
                    "rss_kib": app.rss_kib,
                }
                for app in apps
            ],
            "notes": list(report.apps.notes),
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
                    "rss_kib": browser.rss_kib,
                }
                for browser in browsers
            ],
            "notes": list(report.browsers.notes),
        },
    }


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


def render_table(report: Report) -> str:
    lines: list[str] = []
    free = report.free_memory
    if free is None:
        lines.append("Free memory: unknown (no readable meminfo)")
    else:
        total = (
            ""
            if free.total_kib is None
            else f"; {MEMINFO_TOTAL_FIELD} {_mib(free.total_kib)}"
        )
        lines.append(
            f"Free memory: {_mib(free.free_kib)} ({free.field} from {free.source}{total})"
        )

    lines += [
        "",
        f"Idle chats and workers (waiting, not on a background task, no activity for {_duration(IDLE_AFTER_SECONDS)}"
        " or more):",
    ]
    agents = report.agents.candidates
    if agents is None:
        lines.append("  unknown")
    elif not agents:
        lines.append("  none")
    else:
        lines += _table(
            ("KIND", "NAME", "TITLE", "IDLE", "LAST ACTIVITY (UTC)", "MEMORY"),
            [
                (
                    agent.kind,
                    agent.name,
                    agent.display_name or "-",
                    _duration(agent.idle_seconds),
                    agent.last_activity.astimezone(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                    _mib(agent.rss_kib),
                )
                for agent in agents
            ],
        )
    lines += [f"  note: {note}" for note in report.agents.notes]

    lines += ["", "Apps the workspace may stop (running):"]
    apps = report.apps.candidates
    if apps is None:
        lines.append("  unknown")
    elif not apps:
        lines.append("  none")
    else:
        lines += _table(
            ("NAME", "TITLE", "PROGRAM", "WINDOWS", "MEMORY"),
            [
                (
                    app.name,
                    app.display_name,
                    app.program,
                    str(app.window_count),
                    _mib(app.rss_kib),
                )
                for app in apps
            ],
        )
    lines += [f"  note: {note}" for note in report.apps.notes]

    lines += ["", "Browsers no window shows (running):"]
    browsers = report.browsers.candidates
    if browsers is None:
        lines.append("  unknown")
    elif not browsers:
        lines.append("  none")
    else:
        lines += _table(
            ("NAME", "CONTROLLER", "TABS", "ACTIVE PAGE", "MEMORY"),
            [
                (
                    browser.name,
                    browser.controller,
                    str(browser.tab_count),
                    browser.active_url or "-",
                    _mib(browser.rss_kib),
                )
                for browser in browsers
            ],
        )
    lines += [f"  note: {note}" for note in report.browsers.notes]

    lines += [
        "",
        "Memory is summed RSS, so pages shared between processes count more than once.",
        f"Nothing was stopped. Stopping what the user approves: {README_SECTION}.",
    ]
    return "\n".join(lines) + "\n"


# Entry point


def _run_command(
    argv: Sequence[str], timeout_seconds: float
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout_seconds, check=False
    )


def resolve_browser_service_url() -> str:
    """The browser service's base URL: the env override, the app registry's row, then the default."""
    override = os.environ.get(ENV_BROWSER_SERVICE_URL, "")
    if override:
        return override.rstrip("/")
    registry = app_registry.registry_path()
    if not registry.is_absolute():
        registry = _REPO_ROOT / registry
    return (
        app_registry.read_app_url(registry, BROWSER_APP_NAME)
        or DEFAULT_BROWSER_SERVICE_URL
    )


def busy_chat_check(environ: Mapping[str, str]) -> IsChatBusy:
    """Whether a chat has a live background task, from its marker files."""
    root = markers.marker_root(environ, _REPO_ROOT)
    return lambda chat_id: markers.is_busy(root, chat_id)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List idle chats and workers, the apps, and browsers no window shows, alongside free memory. Stops nothing."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the report as JSON instead of a table",
    )
    args = parser.parse_args()
    sources = Sources(
        proc_dir=_PROC_DIR,
        host_meminfo=HOST_MEMINFO_PATH,
        run_command=_run_command,
        fetch_json=fetch_json_over_http,
        read_supervised_pids=supervised_pids,
        browser_service_url=resolve_browser_service_url(),
        shell_url=os.environ.get(ENV_SHELL_URL, DEFAULT_SHELL_URL).rstrip("/"),
        now=datetime.now(timezone.utc),
        is_chat_busy=busy_chat_check(os.environ),
    )
    report = collect_report(sources)
    output = (
        json.dumps(report_to_json(report), indent=2) + "\n"
        if args.json
        else render_table(report)
    )
    sys.stdout.write(output)


if __name__ == "__main__":
    main()
