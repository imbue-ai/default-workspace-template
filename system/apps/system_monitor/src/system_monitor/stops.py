"""The programs the memory guard (earlyoom) stopped in the last 24 hours, in plain words.

Each stop is a record in the workspace's shed ledger (oom_priority's ``process_shed``), paired
with the guard's own log line for it, which names the full command.
"""

import functools
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from system_monitor.programs import chat_agent_id, is_code_checker
from system_monitor.tail import read_tail

# The guard's own log: the ledger keeps only a thread name ("MainThread"), but this names the
# full command of everything it stopped, and how low memory was.
SHED_LOG = Path("/var/log/supervisor/earlyoom-stderr.log")
# Only the end of each is read: the stops shown are the last 24 hours', and both files only
# grow (the log until it is rotated at 10 MB, the ledger forever). About 1,000 ledger records
# and several thousand log entries.
LEDGER_TAIL_BYTES = 256 * 1024
SHED_LOG_TAIL_BYTES = 1024 * 1024
STOPS_WINDOW = timedelta(hours=24)

# earlyoom's line for each stop: the pid, the process name in quotes, and last its command line.
_SENDING = re.compile(
    r'^sending SIG\w+ to process (\d+) uid \d+ "([^"]*)".*?cmdline "(.*)$'
)


def read_shed_log(log: Path = SHED_LOG) -> list[dict[str, Any]]:
    """Each stop in the memory guard's log, oldest first: its pid, process name, and command.

    The log can run to megabytes, so it is parsed again only when its size or modification
    time changes. Callers must not modify the entries: the same list is handed out until then.
    """
    try:
        status = log.stat()
    except OSError:
        return []
    return _parse_shed_log(log, status.st_mtime_ns, status.st_size)


@functools.lru_cache(maxsize=2)
def _parse_shed_log(log: Path, mtime_ns: int, size: int) -> list[dict[str, Any]]:
    # mtime_ns and size are only the cache key: a change to either reads the file afresh.
    try:
        lines = read_tail(log, SHED_LOG_TAIL_BYTES)
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines:
        if current is not None:
            # A command with newlines runs on until its closing quote; anything else ends it
            # unfinished, and the stop is then left out so no other line is glued onto it.
            if line.startswith(f"process {current['pid']} exited") or line.startswith(
                ("mem avail:", "sending SIG")
            ):
                current = None
            elif line.endswith('"'):
                current["command"] += "\n" + line[:-1]
                out.append(current)
                current = None
                continue
            else:
                current["command"] += "\n" + line
                continue
        if m := _SENDING.match(line):
            entry: dict[str, Any] = {
                "pid": int(m.group(1)),
                "comm": m.group(2),
                "command": m.group(3),
            }
            if entry["command"].endswith('"'):
                entry["command"] = entry["command"][:-1]
                out.append(entry)
            else:
                current = entry
    return out


def describe_command(command: str, names: dict[str, str]) -> str:
    """What a stopped program was, in the user's words."""
    first = command.split("\n", 1)[0]
    if "playwright/driver" in first:
        return "a test browser a chat was using to check an app"
    if first.startswith("claude -p") or " claude -p " in first:
        return "a one-off AI task an app or skill started"
    if agent_id := chat_agent_id(first):
        return (
            f"the {names[agent_id]} chat's agent"
            if agent_id in names
            else "a chat's agent"
        )
    if is_code_checker(first):
        return "a code checker"
    if "tilion" in first or "chrom" in first:
        return "a browser"
    return (
        f"a background program ({Path(first.split()[0]).name})"
        if first.split()
        else "a background program"
    )


def read_ledger(ledger: Path) -> list[dict[str, Any]]:
    """The workspace's record of each stop (``process_shed``), oldest first.

    The ledger only grows (nothing trims it), so like the guard's log it is parsed again only
    when its size or modification time changes.
    """
    try:
        status = ledger.stat()
    except OSError:
        return []
    return _parse_ledger(ledger, status.st_mtime_ns, status.st_size)


@functools.lru_cache(maxsize=2)
def _parse_ledger(ledger: Path, mtime_ns: int, size: int) -> list[dict[str, Any]]:
    try:
        lines = read_tail(ledger, LEDGER_TAIL_BYTES)
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
            when = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if isinstance(record, dict) and record.get("type") == "process_shed":
            records.append({**record, "when": when})
    return records


def recent_stops(
    ledger: Path,
    now: datetime | None = None,
    log: Path = SHED_LOG,
    names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Programs the workspace stopped to free memory in the last 24 hours, newest first.

    Each record is paired with the guard's own log line for it, which gives the command and so
    a plain description. Pairing runs newest first and needs the pid and the process name to
    match: the guard's log is rotated and the ledger is not, so the oldest records lose their
    lines, and taken in that order they find none, where a later stop's line that reused their
    pid would mislabel them. A stop with no line keeps a plainer description and no command.
    """
    now = now or datetime.now(timezone.utc)
    names = names or {}
    entries = read_shed_log(log)
    unused: dict[int, list[int]] = {}
    for index, entry in enumerate(entries):
        unused.setdefault(entry["pid"], []).append(index)
    stops: list[dict[str, Any]] = []
    for record in sorted(read_ledger(ledger), key=lambda r: r["when"], reverse=True):
        entry = None
        candidates = unused.get(record.get("pid"), [])  # oldest first
        for position in range(len(candidates) - 1, -1, -1):
            candidate = entries[candidates[position]]
            if record.get("comm") in (None, candidate["comm"]):
                entry = candidate
                del candidates[position]
                break
        if now - record["when"] > STOPS_WINDOW:
            continue
        agent = record.get("agent_name")
        if entry is not None:
            what = describe_command(entry["command"], names)
        elif agent:
            what = f"the {agent.removesuffix('-Agent')} chat's agent"
        elif record.get("comm") == "claude":
            what = "a chat's agent"
        elif record.get("is_worker"):
            what = "a background agent"
        else:
            what = "a background program"
        stops.append(
            {
                "at": record["when"].isoformat(),
                "what": what,
                "command": entry["command"] if entry else None,
            }
        )
    return stops
