"""Announce a just-registered app on the services stream, ahead of the shell.

The minds desktop and ``mngr forward`` route to a workspace's apps only once a ``service_registered``
event for them is on ``$MNGR_AGENT_STATE_DIR/events/services/events.jsonl``. The shell writes that
stream (``imbue.system_interface.app_announcements``), but only after it has imported and read the
registry, which at boot is seconds after its own and the chat app's rows were registered. A program
line that registers with ``forward_port.py --announce`` also appends its row's event right away, in
the shape the shell writes, so the app is routable from its first moment; the shell announces every
row again at its first read, and an app that binds its port first (``listen_then_exec.py``) answers
whatever arrives in the meantime once it serves.

A row whose latest event in the stream's tail already says the same is not written again, so an app
restarting in a loop does not grow the stream. Stdlib only, like the rest of this package.
"""

import json
import logging
import os
import secrets
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final

_logger = logging.getLogger(__name__)

ENV_AGENT_STATE_DIR: Final[str] = "MNGR_AGENT_STATE_DIR"
ANNOUNCEMENTS_REL: Final[str] = "events/services/events.jsonl"
SOURCE: Final[str] = "services"
REGISTERED_TYPE: Final[str] = "service_registered"
DEREGISTERED_TYPE: Final[str] = "service_deregistered"
# How much of the stream's end is read to find the row's latest event: well past the few dozen
# rows a boot writes, so a row is written again only when it changed or the stream moved on.
_TAIL_BYTES: Final[int] = 256 * 1024
# The fields of a registration that describe the row, as opposed to the envelope.
_ROW_FIELDS: Final[tuple[str, ...]] = (
    "service",
    "url",
    "label",
    "icon",
    "display_name",
    "system",
    "internal",
)


def announcements_path(environ: Mapping[str, str]) -> Path | None:
    """Where the stream lives, or None outside a workspace (no agent state directory)."""
    state_dir = environ.get(ENV_AGENT_STATE_DIR)
    if not state_dir:
        return None
    return Path(state_dir) / ANNOUNCEMENTS_REL


def nanosecond_timestamp(nanoseconds: int) -> str:
    """The envelope's timestamp: UTC, nanosecond precision, ``Z``-suffixed."""
    seconds, remainder = divmod(nanoseconds, 1_000_000_000)
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{remainder:09d}Z"
    )


def registration_event(
    row: Mapping[str, object], timestamp: str, event_id: str
) -> dict[str, object]:
    """The ``service_registered`` event for one registry row, field for field the shell's
    ``AppRegisteredAnnouncement`` (an absent icon or display name is empty, an absent flag false)."""
    return {
        "timestamp": timestamp,
        "type": REGISTERED_TYPE,
        "event_id": event_id,
        "source": SOURCE,
        "service": str(row["name"]),
        "url": str(row["url"]),
        "label": str(row.get("label") or ""),
        "icon": str(row.get("icon") or ""),
        "display_name": str(row.get("display_name") or ""),
        "system": bool(row.get("system", False)),
        "internal": bool(row.get("internal", False)),
    }


def latest_event_for(events_path: Path, name: str) -> dict[str, object] | None:
    """The newest registration or deregistration of ``name`` in the stream's tail, or None."""
    try:
        with events_path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - _TAIL_BYTES))
            tail = stream.read()
    except FileNotFoundError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(event, dict)
            and event.get("service") == name
            and event.get("type") in (REGISTERED_TYPE, DEREGISTERED_TYPE)
        ):
            return event
    return None


def is_already_announced(events_path: Path, event: Mapping[str, object]) -> bool:
    latest = latest_event_for(events_path, str(event["service"]))
    return (
        latest is not None
        and latest.get("type") == REGISTERED_TYPE
        and all(latest.get(field) == event[field] for field in _ROW_FIELDS)
    )


def announce_row(row: Mapping[str, object], events_path: Path) -> bool:
    """Append ``row``'s registration to the stream unless its tail already says the same; whether it wrote.

    A stream that cannot be written is logged and skipped: the shell announces the row at its first read anyway.
    """
    event = registration_event(
        row,
        timestamp=nanosecond_timestamp(time.time_ns()),
        event_id=f"evt-{secrets.token_hex(16)}",
    )
    try:
        if is_already_announced(events_path, event):
            return False
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
            )
    except OSError as error:
        _logger.warning(
            "could not announce %s to %s: %s", row["name"], events_path, error
        )
        return False
    return True
