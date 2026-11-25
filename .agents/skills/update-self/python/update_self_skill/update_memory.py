"""What an update could stop to free memory, asked of the workspace tree's own memory tool.

The tool reads what the running workspace wrote: mngr's listing, the agent-pid registry, the
shell's and the Screen service's answers, the memory guard's program file. Only the copy in the
workspace's own tree is sure to match those, so that is the copy run here, never one from the
target release; the tool's ``--json`` output is then the one thing that crosses versions. Each
part of it is taken by key, and a part this workspace's tool does not print is reported as
unknown, never as "nothing to stop". ``update_memory_test.py`` holds what is read to each
release's output.

Stdlib-only, like its siblings here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

# own-process: other-tree -- the memory tool reads what the running workspace wrote, so the copy in that workspace's tree is the one that matches it
WORKSPACE_MEMORY_REL = (
    Path("system") / "services" / "oom_priority" / "bin" / "workspace_memory.py"
)
# What the tool was called before it became workspace_memory.py.
MEMORY_CANDIDATES_REL = (
    Path("system") / "services" / "oom_priority" / "bin" / "memory_candidates.py"
)

# Longer than the tool gives its own `mngr list` and service calls, so it answers first.
TOOL_TIMEOUT_SECONDS = 120.0
_KIB_PER_MIB = 1024

RunTool = Callable[[Sequence[str], Path, float], "subprocess.CompletedProcess[str]"]


class FreeMemory(NamedTuple):
    """Free memory as the memory guard reads it."""

    free_kib: int
    field: str | None
    source: str | None
    # What is left before the guard starts stopping processes; None where the tool does not say.
    room_kib: int | None


class AgentRow(NamedTuple):
    """An idle chat or background agent."""

    kind: str
    name: str
    title: str | None
    idle_seconds: float | None
    memory_kib: int | None


class AppRow(NamedTuple):
    """A running app the workspace may stop. ``program`` is what ``run-status stop-apps`` takes."""

    program: str
    title: str | None
    window_count: int | None
    stops_when_no_windows: bool | None
    memory_kib: int | None


class BrowserRow(NamedTuple):
    """A browser running while no window shows it."""

    name: str
    memory_kib: int | None


class MemoryReading(NamedTuple):
    """What the workspace's memory tool listed. A section is None when it could not be listed,
    and ``unknown`` says why for each."""

    tool: Path | None
    free: FreeMemory | None
    agents: tuple[AgentRow, ...] | None
    apps: tuple[AppRow, ...] | None
    browsers: tuple[BrowserRow, ...] | None
    unknown: tuple[str, ...]
    # Whether the sizes are resident sizes, which count every file a process maps.
    is_resident_size: bool


def tool_argv(repo_root: Path) -> tuple[Path, list[str]] | None:
    """The workspace tree's memory tool and the command that prints its listing as JSON, or None
    when the tree has neither."""
    current = repo_root / WORKSPACE_MEMORY_REL
    if current.is_file():
        return WORKSPACE_MEMORY_REL, [sys.executable, str(current), "list", "--json"]
    # CLEANUP: drop this branch, and the resident sizes it can report, once update-self's floor,
    # the oldest release the app updates from, reaches the first release that ships
    # workspace_memory.py: only a workspace older than that gets here.
    earlier = repo_root / MEMORY_CANDIDATES_REL
    if earlier.is_file():
        return MEMORY_CANDIDATES_REL, [sys.executable, str(earlier), "--json"]
    return None


def _run_tool(
    argv: Sequence[str], cwd: Path, timeout: float
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def read_memory(repo_root: Path, run_tool: RunTool = _run_tool) -> MemoryReading:
    """Run the workspace tree's memory tool and take what an update needs from its listing."""
    found = tool_argv(repo_root)
    if found is None:
        return _nothing_read(
            None,
            "this workspace's version has no memory listing, so only chats can be stopped "
            "(`run-status stop-chats --agent`); say so in the results message",
        )
    tool, argv = found
    try:
        result = run_tool(argv, repo_root, TOOL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return _nothing_read(
            tool, f"{tool} did not answer within {TOOL_TIMEOUT_SECONDS:g}s"
        )
    except OSError as error:
        return _nothing_read(tool, f"could not run {tool}: {error}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return _nothing_read(
            tool,
            f"{tool} exited {result.returncode}: {detail[-1] if detail else 'no output'}",
        )
    try:
        document = json.loads(result.stdout)
    except ValueError as error:
        return _nothing_read(tool, f"{tool} did not print JSON: {error}")
    if not isinstance(document, dict):
        return _nothing_read(tool, f"{tool} printed JSON that is not a listing")
    return parse_listing(tool, document)


def _nothing_read(tool: Path | None, why: str) -> MemoryReading:
    return MemoryReading(
        tool=tool,
        free=None,
        agents=None,
        apps=None,
        browsers=None,
        unknown=(why,),
        is_resident_size=False,
    )


def parse_listing(tool: Path, document: Mapping[str, Any]) -> MemoryReading:
    """The parts of the tool's JSON an update reads, each taken only where it has the expected shape."""
    unknown: list[str] = []
    resident: list[bool] = []
    agents = _section(document, "agents", "idle chats and background agents", unknown)
    apps = _section(document, "apps", "apps", unknown)
    browsers = _section(document, "browsers", "browsers", unknown)
    free = _free_memory(document.get("free_memory"))
    if free is None:
        unknown.append("free memory: the tool could not read it")
    return MemoryReading(
        tool=tool,
        free=free,
        agents=None
        if agents is None
        else tuple(
            AgentRow(
                kind=_text(row.get("kind")) or "agent",
                name=name,
                title=_text(row.get("display_name")),
                idle_seconds=_number(row.get("idle_seconds")),
                memory_kib=_memory_kib(row, resident),
            )
            for row in agents
            if (name := _text(row.get("name"))) is not None
        ),
        apps=None
        if apps is None
        else tuple(
            AppRow(
                program=program,
                title=_text(row.get("display_name")) or _text(row.get("name")),
                window_count=_integer(row.get("window_count")),
                stops_when_no_windows=stops
                if isinstance(stops := row.get("stop_when_no_windows"), bool)
                else None,
                memory_kib=_memory_kib(row, resident),
            )
            for row in apps
            if (program := _text(row.get("program"))) is not None
        ),
        browsers=None
        if browsers is None
        else tuple(
            BrowserRow(name=name, memory_kib=_memory_kib(row, resident))
            for row in browsers
            if (name := _text(row.get("name"))) is not None
        ),
        unknown=tuple(unknown),
        is_resident_size=any(resident),
    )


def _section(
    document: Mapping[str, Any], key: str, label: str, unknown_out: list[str]
) -> list[Mapping[str, Any]] | None:
    """A section's candidate rows, or None with the reason added to ``unknown_out``: the tool
    predates the section, or could not read its source and says why in its notes."""
    section = document.get(key)
    if not isinstance(section, dict):
        unknown_out.append(f"{label}: this workspace's version does not list them")
        return None
    candidates = section.get("candidates")
    if not isinstance(candidates, list):
        notes = section.get("notes")
        reasons = (
            [note for note in notes if isinstance(note, str)]
            if isinstance(notes, list)
            else []
        )
        unknown_out.append(
            f"{label}: {'; '.join(reasons) or 'the tool could not list them'}"
        )
        return None
    return [row for row in candidates if isinstance(row, dict)]


def _free_memory(raw: Any) -> FreeMemory | None:
    if not isinstance(raw, dict):
        return None
    free_kib = _integer(raw.get("free_kib"))
    if free_kib is None:
        return None
    return FreeMemory(
        free_kib=free_kib,
        field=_text(raw.get("field")),
        source=_text(raw.get("source")),
        room_kib=_integer(raw.get("room_kib")),
    )


def _memory_kib(row: Mapping[str, Any], resident_out: list[bool]) -> int | None:
    """A row's memory: its anonymous memory, else the resident size earlier releases reported
    (noted in ``resident_out``)."""
    anonymous_kib = _integer(row.get("anonymous_kib"))
    if anonymous_kib is not None or "anonymous_kib" in row:
        return anonymous_kib
    rss_kib = _integer(row.get("rss_kib"))
    if rss_kib is not None:
        resident_out.append(True)
    return rss_kib


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _mib(kib: int | None) -> str:
    return "?" if kib is None else f"{kib // _KIB_PER_MIB} MiB"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    if not rows:
        return ["  none"]
    widths = [max(len(cell) for cell in column) for column in zip(header, *rows)]
    return [
        "  " + "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
        for row in (header, *rows)
    ]


def render_reading(reading: MemoryReading) -> str:
    """The listing as the lead reads it: free memory, then what could be stopped, by how it is stopped."""
    free = reading.free
    if free is None:
        lines = ["Free memory: unknown"]
    else:
        origin = (
            f" ({free.field} from {free.source})" if free.field and free.source else ""
        )
        room = (
            "; how much of it is left before the memory guard starts stopping processes is "
            "unknown on this workspace's version"
            if free.room_kib is None
            else f"; {_mib(free.room_kib)} of it before the memory guard starts stopping processes"
        )
        lines = [f"Free memory: {_mib(free.free_kib)}{origin}{room}"]

    lines += [
        "",
        "Idle chats and background agents (`run-status stop-chats --agent <NAME>`):",
    ]
    lines += (
        ["  unknown"]
        if reading.agents is None
        else _table(
            ("KIND", "NAME", "TITLE", "IDLE", "MEMORY"),
            [
                (
                    agent.kind,
                    agent.name,
                    agent.title or "-",
                    _duration(agent.idle_seconds),
                    _mib(agent.memory_kib),
                )
                for agent in reading.agents
            ],
        )
    )

    lines += ["", "Apps (`run-status stop-apps <PROGRAM>`):"]
    lines += (
        ["  unknown"]
        if reading.apps is None
        else _table(
            ("PROGRAM", "TITLE", "WINDOWS", "AUTO-STOP", "MEMORY"),
            [
                (
                    app.program,
                    app.title or "-",
                    "?" if app.window_count is None else str(app.window_count),
                    "?"
                    if app.stops_when_no_windows is None
                    else ("yes" if app.stops_when_no_windows else "no"),
                    _mib(app.memory_kib),
                )
                for app in reading.apps
            ],
        )
    )

    lines += ["", "Browsers no window shows (the browser service's stop):"]
    lines += (
        ["  unknown"]
        if reading.browsers is None
        else _table(
            ("NAME", "MEMORY"),
            [(browser.name, _mib(browser.memory_kib)) for browser in reading.browsers],
        )
    )

    lines.append("")
    lines += [f"unknown -- {why}" for why in reading.unknown]
    if reading.is_resident_size:
        lines.append(
            "The sizes are resident sizes, which this workspace's version reports: they count "
            "every file a process maps, so stopping something frees less than its row says."
        )
    if reading.tool is not None:
        lines.append(
            f"Read by this workspace's own {reading.tool}. Nothing was stopped."
        )
    return "\n".join(lines).rstrip("\n") + "\n"
