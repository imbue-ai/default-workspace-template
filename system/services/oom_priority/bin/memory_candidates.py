#!/usr/bin/env python3
"""List what could be stopped to free memory: idle chats and workers, and browsers no window shows.

A lead runs this after one of its agents reports a shed (see the README's "Memory candidates"),
shows the user the list next to the free memory, and stops only what the user approves. This
command only reads: it never stops, kills, or re-tags anything.

It joins three sources, each read independently so one failing leaves the others intact:

- free memory, from ``/proc/meminfo`` (``MemAvailable``, else ``MemFree``; the field read is named);
- agents, from ``mngr list --provider local`` rendered through a ``--format`` template: a chat
  (``user_created``) or worker (``agent_created``) whose state is ``WAITING`` and whose latest
  activity is at least ``IDLE_AFTER_SECONDS`` old. Its memory is the summed RSS of its process trees, rooted at the pid
  mngr reports plus every live pid the agent-pid registry holds for it;
- browsers, from the browser service's ``GET /browsers``: a ``running`` browser that no desktop
  window shows, per the shell's ``GET /api/desktops``. Its memory is the summed RSS of every
  Chromium process on its profile.

A source that cannot be read is reported as unknown (``null`` candidates plus a note), never as
"nothing to stop".

Self-contained beyond the stdlib-only ``oom_priority`` package (imported via a ``sys.path``
insert), so it runs under a plain ``python3``.
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

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "src")
)

from oom_priority import app_registry
from oom_priority.agent_identity import (
    CHAT_LABEL,
    PRIMARY_LABEL,
    WORKER_LABEL,
    is_label_true,
)
from oom_priority.proctree import list_descendant_pids
from oom_priority.registry import live_pids_by_agent_id

# An agent counts as idle once its turn has ended (``WAITING``) and nothing -- a message to it,
# its own work, a restart -- has happened for this long. Shorter than this, the user is likely
# still reading its reply.
IDLE_AFTER_SECONDS: Final[float] = 15 * 60
IDLE_AGENT_STATE: Final[str] = "WAITING"
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

# The shell's address and its desktops document, as app_manifest.shell_windows reads them.
ENV_SHELL_URL: Final[str] = "MINDS_WORKSPACE_SERVER_URL"
DEFAULT_SHELL_URL: Final[str] = "http://127.0.0.1:8000"
DESKTOPS_ROUTE: Final[str] = "/api/desktops"

MEMINFO_PREFERRED_FIELD: Final[str] = "MemAvailable"
MEMINFO_FALLBACK_FIELD: Final[str] = "MemFree"
MEMINFO_TOTAL_FIELD: Final[str] = "MemTotal"
RSS_STATUS_FIELD: Final[str] = "VmRSS"
KIB_PER_MIB: Final[int] = 1024
README_SECTION: Final[str] = 'system/services/oom_priority/README.md, "Memory candidates"'

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
_PROC_DIR: Final[Path] = Path("/proc")

RunCommand = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]
FetchJson = Callable[[str, float], Any]


class FreeMemory(NamedTuple):
    """Free memory as ``/proc/meminfo`` states it, naming the field it came from."""

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


class BrowserSection(NamedTuple):
    """The unviewed browsers, or None when they could not be determined, plus what went wrong."""

    candidates: tuple[BrowserCandidate, ...] | None
    notes: tuple[str, ...]


class Report(NamedTuple):
    """Everything the command found."""

    generated_at: datetime
    free_memory: FreeMemory | None
    agents: AgentSection
    browsers: BrowserSection


class Sources(NamedTuple):
    """Where the command reads from; injectable so tests run against a fake ``/proc`` and fake services."""

    proc_dir: Path
    run_command: RunCommand
    fetch_json: FetchJson
    browser_service_url: str
    shell_url: str
    now: datetime


# Free memory


def read_free_memory(meminfo_path: Path) -> FreeMemory | None:
    """``MemAvailable`` (else ``MemFree``) and ``MemTotal``, or None when the file is absent or has neither."""
    try:
        text = meminfo_path.read_text()
    except OSError:
        return None
    values_kib: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            values_kib[key.strip()] = int(fields[0])
    for field in (MEMINFO_PREFERRED_FIELD, MEMINFO_FALLBACK_FIELD):
        if field in values_kib:
            return FreeMemory(field=field, free_kib=values_kib[field], total_kib=values_kib.get(MEMINFO_TOTAL_FIELD))
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
    readings = [rss for rss in (read_rss_kib(pid, proc_dir) for pid in sorted(pids)) if rss is not None]
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
            _parse_timestamp(values[field]) for field in ("user_activity_time", "agent_activity_time", "start_time")
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


def run_mngr_list(run_command: RunCommand) -> tuple[list[ListedAgent] | None, list[str]]:
    """The agents ``mngr list`` rendered, or None when it could not tell, plus notes on what went wrong.

    With ``--on-error continue`` mngr still renders every agent it could list when something fails,
    reports the failure on stderr, and exits non-zero; those agents are kept with the failure as a
    note. A non-zero exit with no agents at all is "unknown", not "no agents".
    """
    try:
        result = run_command(MNGR_LIST_ARGV, MNGR_LIST_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, [f"could not run `mngr list`: {error}"]
    agents = [agent for agent in (parse_listed_agent(line) for line in result.stdout.splitlines()) if agent is not None]
    if result.returncode == 0:
        return agents, []
    stderr_lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    note = f"mngr list exited {result.returncode}: {'; '.join(stderr_lines) or 'no output'}"
    return (agents if agents else None), [note]


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
) -> list[AgentCandidate]:
    """The idle chats and workers, largest memory first."""
    candidates: list[AgentCandidate] = []
    for agent in agents:
        kind = agent_kind(agent.labels)
        if kind is None or agent.state != IDLE_AGENT_STATE or agent.last_activity is None:
            continue
        idle_seconds = (now - agent.last_activity).total_seconds()
        if idle_seconds < IDLE_AFTER_SECONDS:
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
                rss_kib=process_trees_rss_kib(root_pids, proc_dir) if root_pids else None,
            )
        )
    return sorted(candidates, key=lambda c: (c.rss_kib is None, -(c.rss_kib or 0), -c.idle_seconds))


def collect_agent_section(sources: Sources) -> AgentSection:
    agents, notes = run_mngr_list(sources.run_command)
    if agents is None:
        return AgentSection(candidates=None, notes=tuple(notes))
    registry_pids = live_pids_by_agent_id(is_alive=lambda pid: (sources.proc_dir / str(pid)).is_dir())
    candidates = idle_agent_candidates(agents, registry_pids, sources.proc_dir, sources.now)
    return AgentSection(candidates=tuple(candidates), notes=tuple(notes))


# Browsers


def fetch_json_over_http(url: str, timeout_seconds: float) -> Any:
    """GET ``url`` and parse its JSON body. Raises ``OSError`` (a ``URLError`` for an unreachable
    server or an error status), ``http.client.HTTPException``, or ``ValueError`` for a non-JSON body."""
    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
        return json.loads(response.read())


def browser_names_shown_by_windows(desktops: Any) -> set[str] | None:
    """The browsers some desktop window shows, from the shell's desktops document, or None when the
    document is not shaped as one (which, like an unreachable shell, means "unknown", never "none").

    Mirrors ``app_manifest.shell_windows.window_paths_of_app``: a window's own path and each
    client's path of it all count.
    """
    if not isinstance(desktops, dict) or not isinstance(desktops.get("desktops"), list):
        return None
    shown: set[str] = set()
    for desktop in desktops["desktops"]:
        if not isinstance(desktop, dict) or not isinstance(desktop.get("windows"), list):
            return None
        for window in desktop["windows"]:
            if not isinstance(window, dict):
                return None
            if window.get("app") != BROWSER_APP_NAME:
                continue
            client_paths = window.get("client_paths", {})
            paths = [window.get("path"), *(client_paths.values() if isinstance(client_paths, dict) else ())]
            for path in paths:
                if not isinstance(path, str):
                    continue
                values = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get(BROWSER_SESSION_QUERY_KEY)
                if values:
                    shown.add(values[0])
    return shown


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
        if any(arg.startswith(USER_DATA_DIR_FLAG) and arg.rstrip("/").endswith(profile_suffix) for arg in argv):
            pids.append(int(entry.name))
    return tuple(sorted(pids))


def _browser_controller(browser: Mapping[str, Any]) -> str:
    """Who controls the browser, in the fleet CLI's ``ls`` words."""
    if browser.get("controller") == "agent":
        return f"agent {browser.get('owner_name') or browser.get('owner_agent_id') or '?'}"
    return "human" if browser.get("human_pinned") else "free"


def unviewed_browser_candidates(
    browsers: Sequence[Mapping[str, Any]], shown_names: set[str], proc_dir: Path
) -> list[BrowserCandidate]:
    """The running browsers no window shows, largest memory first."""
    candidates: list[BrowserCandidate] = []
    for browser in browsers:
        name = browser.get("id")
        if not isinstance(name, str) or browser.get("lifecycle") != BROWSER_RUNNING_LIFECYCLE:
            continue
        if name in shown_names:
            continue
        raw_tabs = browser.get("tabs")
        tabs = [tab for tab in raw_tabs if isinstance(tab, dict)] if isinstance(raw_tabs, list) else []
        active = next((tab for tab in tabs if tab.get("active")), None)
        active_url = active.get("url") if active is not None else None
        pids = chromium_pids_for_browser(name, proc_dir)
        candidates.append(
            BrowserCandidate(
                name=name,
                controller=_browser_controller(browser),
                tab_count=len(tabs),
                active_url=active_url if isinstance(active_url, str) and active_url else None,
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


def collect_browser_section(sources: Sources) -> BrowserSection:
    fleet_url = f"{sources.browser_service_url}/browsers"
    fleet, fleet_error = _fetch(sources.fetch_json, fleet_url)
    if fleet_error is not None:
        return BrowserSection(None, (f"could not reach the browser service at {fleet_url}: {fleet_error}",))
    browsers = fleet.get("browsers") if isinstance(fleet, dict) else None
    if not isinstance(browsers, list):
        return BrowserSection(None, (f"the browser service at {fleet_url} answered without a browsers list",))
    browser_records = [browser for browser in browsers if isinstance(browser, dict)]
    if not any(browser.get("lifecycle") == BROWSER_RUNNING_LIFECYCLE for browser in browser_records):
        return BrowserSection((), ())

    # Only a running browser holds memory, and only the shell knows which ones a window shows.
    desktops_url = f"{sources.shell_url}{DESKTOPS_ROUTE}"
    desktops, desktops_error = _fetch(sources.fetch_json, desktops_url)
    shown = browser_names_shown_by_windows(desktops) if desktops_error is None else None
    if shown is None:
        reason = desktops_error or "not a desktops document"
        return BrowserSection(
            None, (f"could not read the shell's windows at {desktops_url} ({reason}), so which browsers no window shows is unknown",)
        )
    return BrowserSection(tuple(unviewed_browser_candidates(browser_records, shown, sources.proc_dir)), ())


# The report


def collect_report(sources: Sources) -> Report:
    return Report(
        generated_at=sources.now,
        free_memory=read_free_memory(sources.proc_dir / "meminfo"),
        agents=collect_agent_section(sources),
        browsers=collect_browser_section(sources),
    )


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def report_to_json(report: Report) -> dict[str, object]:
    free = report.free_memory
    agents = report.agents.candidates
    browsers = report.browsers.candidates
    return {
        "generated_at": _iso(report.generated_at),
        "idle_after_seconds": IDLE_AFTER_SECONDS,
        "free_memory": None
        if free is None
        else {"field": free.field, "free_kib": free.free_kib, "total_kib": free.total_kib, "source": "/proc/meminfo"},
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
    return ["  " + "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in (header, *rows)]


def render_table(report: Report) -> str:
    lines: list[str] = []
    free = report.free_memory
    if free is None:
        lines.append("Free memory: unknown (no readable /proc/meminfo)")
    else:
        total = "" if free.total_kib is None else f"; {MEMINFO_TOTAL_FIELD} {_mib(free.total_kib)}"
        lines.append(f"Free memory: {_mib(free.free_kib)} ({free.field} from /proc/meminfo{total})")

    lines += ["", f"Idle chats and workers (waiting, no activity for {_duration(IDLE_AFTER_SECONDS)} or more):"]
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
                    agent.last_activity.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    _mib(agent.rss_kib),
                )
                for agent in agents
            ],
        )
    lines += [f"  note: {note}" for note in report.agents.notes]

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
                (browser.name, browser.controller, str(browser.tab_count), browser.active_url or "-", _mib(browser.rss_kib))
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


def _run_command(argv: Sequence[str], timeout_seconds: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout_seconds, check=False)


def resolve_browser_service_url() -> str:
    """The browser service's base URL: the env override, the app registry's row, then the default."""
    override = os.environ.get(ENV_BROWSER_SERVICE_URL, "")
    if override:
        return override.rstrip("/")
    registry = app_registry.registry_path()
    if not registry.is_absolute():
        registry = _REPO_ROOT / registry
    return app_registry.read_app_url(registry, BROWSER_APP_NAME) or DEFAULT_BROWSER_SERVICE_URL


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List idle chats and workers, and browsers no window shows, alongside free memory. Stops nothing."
    )
    parser.add_argument("--json", action="store_true", help="Print the report as JSON instead of a table")
    args = parser.parse_args()
    sources = Sources(
        proc_dir=_PROC_DIR,
        run_command=_run_command,
        fetch_json=fetch_json_over_http,
        browser_service_url=resolve_browser_service_url(),
        shell_url=os.environ.get(ENV_SHELL_URL, DEFAULT_SHELL_URL).rstrip("/"),
        now=datetime.now(timezone.utc),
    )
    report = collect_report(sources)
    output = json.dumps(report_to_json(report), indent=2) + "\n" if args.json else render_table(report)
    sys.stdout.write(output)


if __name__ == "__main__":
    main()
