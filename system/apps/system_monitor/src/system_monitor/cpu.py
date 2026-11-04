"""The CPU readings and the graphs' history, read from the files the resource sampler writes
(``oom_priority.resource_sampler``; ``docs/system/specs/system-monitor.md`` section 3.3): the latest
reading with each process's share, the last 10 minutes, the day of minute lines, and the busy stretches."""

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, NamedTuple

from imbue.imbue_common.event_envelope import parse_iso_timestamp
from oom_priority import resource_sampler

from system_monitor.tail import read_tail

RECENT_PATH: Final[Path] = resource_sampler.STATE_DIR / resource_sampler.RECENT_FILENAME
HISTORY_PATH: Final[Path] = (
    resource_sampler.STATE_DIR / resource_sampler.HISTORY_FILENAME
)
STRETCHES_PATH: Final[Path] = (
    resource_sampler.STATE_DIR / resource_sampler.STRETCH_EVENTS_RELATIVE_PATH
)
# A reading older than this is no reading: the sampler is down or the machine was paused.
FRESH_SECONDS: Final[float] = 30.0
# The spans the history route serves and how far apart their points are.
SPANS: Final[dict[str, tuple[int, int]]] = {"1h": (3600, 60), "24h": (24 * 3600, 300)}
STRETCHES_WINDOW: Final[timedelta] = timedelta(hours=24)
# Only the end of the stretch log is read: a day holds at most a few dozen stretches.
STRETCHES_TAIL_BYTES: Final[int] = 256 * 1024


class RecentReading(NamedTuple):
    """The sampler's latest reading: the CPU in use, each process's share, and the last 10 minutes."""

    sampled_at: float
    cores: int
    usage: float
    by_pid: dict[int, float]
    samples: list[dict[str, Any]]


def read_recent(
    path: Path = RECENT_PATH, now: float | None = None
) -> RecentReading | None:
    """The latest reading, or None when the file is missing, unreadable, or older than ``FRESH_SECONDS``."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if (
        not isinstance(data, dict)
        or data.get("version") != resource_sampler.RECENT_FILE_VERSION
    ):
        return None
    sampled_at = data.get("sampled_at")
    moment = now if now is not None else datetime.now(timezone.utc).timestamp()
    if not isinstance(sampled_at, (int, float)) or moment - sampled_at > FRESH_SECONDS:
        return None
    cpu = data.get("cpu") if isinstance(data.get("cpu"), dict) else {}
    by_pid = {
        int(pid): float(share)
        for pid, share in (cpu.get("by_pid") or {}).items()
        if str(pid).isdigit() and isinstance(share, (int, float))
    }
    samples = [
        {"t": int(raw[0]), "cpu": float(raw[1]), "memory": float(raw[2])}
        for raw in data.get("samples", [])
        if isinstance(raw, list)
        and len(raw) == 3
        and all(isinstance(value, (int, float)) for value in raw)
    ]
    cores, usage = data.get("cores"), cpu.get("usage")
    return RecentReading(
        sampled_at=float(sampled_at),
        cores=int(cores) if isinstance(cores, (int, float)) and cores >= 1 else 1,
        usage=float(usage) if isinstance(usage, (int, float)) else 0.0,
        by_pid=by_pid,
        samples=samples,
    )


def read_history(
    span: str, path: Path = HISTORY_PATH, now_ms: int | None = None
) -> list[dict[str, Any]]:
    """The minute lines over ``span`` as graph points ``step_seconds`` apart (means of the minutes in each step),
    oldest first; a step with no minute lines is left out, so the graph shows a gap there."""
    span_seconds, step_seconds = SPANS[span]
    end_ms = (
        now_ms
        if now_ms is not None
        else int(datetime.now(timezone.utc).timestamp() * 1000)
    )
    start_ms = end_ms - span_seconds * 1000
    buckets: dict[int, list[tuple[float, float]]] = {}
    for line in _read_lines(path):
        time_ms, cpu, memory = line.get("t"), line.get("cpu"), line.get("memory")
        if (
            not isinstance(time_ms, (int, float))
            or not isinstance(cpu, (int, float))
            or not isinstance(memory, (int, float))
        ):
            continue
        if time_ms <= start_ms or time_ms > end_ms:
            continue
        bucket = int((time_ms - start_ms - 1) // (step_seconds * 1000))
        buckets.setdefault(bucket, []).append((float(cpu), float(memory)))
    points: list[dict[str, Any]] = []
    for bucket in sorted(buckets):
        values = buckets[bucket]
        points.append(
            {
                "t": start_ms + (bucket + 1) * step_seconds * 1000,
                "cpu": round(sum(cpu for cpu, _ in values) / len(values), 4),
                "memory": round(sum(memory for _, memory in values) / len(values), 4),
            }
        )
    return points


def _read_lines(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text()
    except OSError:
        return []
    lines: list[dict[str, Any]] = []
    for raw in text.splitlines():
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            lines.append(parsed)
    return lines


def recent_stretches(
    path: Path = STRETCHES_PATH,
    now: datetime | None = None,
    chat_names: Mapping[str, str] | None = None,
    app_names: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The busy stretches of the last 24 hours as the page's events, newest first, with the chats and apps named."""
    moment = now or datetime.now(timezone.utc)
    chat_names = chat_names or {}
    app_names = app_names or {}
    try:
        lines = read_tail(path, STRETCHES_TAIL_BYTES)
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for raw in lines:
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if (
            not isinstance(record, dict)
            or record.get("type") != resource_sampler.STRETCH_EVENT_TYPE
        ):
            continue
        started = parse_iso_timestamp(str(record.get("started_at", "")))
        duration = record.get("duration_seconds")
        if (
            started is None
            or not isinstance(duration, (int, float))
            or moment - started > STRETCHES_WINDOW
        ):
            continue
        top = [
            {
                "name": _cause_name(cause, chat_names, app_names),
                "share": float(cause["share"]),
            }
            for cause in record.get("top", [])
            if isinstance(cause, dict) and isinstance(cause.get("share"), (int, float))
        ]
        events.append(
            {
                "at": started.isoformat(),
                "kind": "busy",
                "what": describe_stretch(int(duration), top),
                "duration_seconds": int(duration),
                "top": top,
            }
        )
    events.sort(key=lambda event: event["at"], reverse=True)
    return events


def _cause_name(
    cause: Mapping[str, Any],
    chat_names: Mapping[str, str],
    app_names: Mapping[str, str],
) -> str:
    if cause.get("kind") == "chat":
        agent_id = str(cause.get("agent_id", ""))
        return (
            f"the {chat_names[agent_id]} chat" if agent_id in chat_names else "a chat"
        )
    program = str(cause.get("program", ""))
    return app_names.get(program, program or "a program")


def describe_stretch(duration_seconds: int, top: list[dict[str, Any]]) -> str:
    """A stretch in plain words: how long the CPU stayed busy, and who used most of it."""
    minutes = max(1, round(duration_seconds / 60))
    head = f"CPU above 90% for {minutes} minute{'s' if minutes != 1 else ''}"
    if not top:
        return head
    return f"{head} (mostly {top[0]['name']})"
