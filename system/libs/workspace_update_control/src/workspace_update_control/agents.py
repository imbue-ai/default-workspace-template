"""The workspace's agents, read from the events file ``mngr observe`` writes, with no ``mngr`` command.

The workspace's update routes are served by the shell, which runs no ``mngr``, so they read the agents
from the observer's append-only JSONL file as the shell's avatar does: a full snapshot of every agent
every five minutes, one agent's state whenever its host shows activity, and a removal when an agent
is destroyed. The fold reads from the end of the file back to the most recent snapshot and applies
every later state and removal on top.

The answer has the shape of the dispatcher's own listing (``dispatcher.list_agents``), so the
functions the routes share with the tick take either. Where the file cannot vouch for the agents (it
is missing, holds no snapshot, went stale, or does not list the workspace's own services agent) the
answer is None: every caller reads that as "the agents are unknown", never as "there are none".
"""

import json
import os
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Final

from workspace_update_control.support import as_json_object

# mngr's conventions, duplicated rather than imported: the host directory's environment variable and
# fallback, the observer's event file under it, and the event types the fold reads.
ENV_MNGR_HOST_DIR: Final[str] = "MNGR_HOST_DIR"
_DEFAULT_MNGR_HOST_DIRNAME: Final[str] = ".mngr"
_AGENT_EVENTS_RELATIVE_PATH: Final[str] = os.path.join(
    "events", "mngr", "agents", "events.jsonl"
)
_FULL_STATE_EVENT_TYPE: Final[str] = "AGENTS_FULL_STATE"
_AGENT_STATE_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {"AGENT_STATE", "AGENT_STATE_CHANGE"}
)
_AGENT_REMOVED_EVENT_TYPE: Final[str] = "AGENT_REMOVED"
# Twice the observer's snapshot interval: past it nothing is writing the file.
_STALE_AFTER: Final[timedelta] = timedelta(minutes=10)
_TAIL_BLOCK_BYTES: Final[int] = 256 * 1024
# The labels the dispatcher's listing carries, under the keys it gives them.
_LISTED_LABELS: Final[tuple[str, ...]] = (
    "is_primary",
    "chat_id",
    "update_dispatch_attempt",
)


def agent_events_path(environ: Mapping[str, str]) -> str:
    """Where the observer's agents event file is: under ``MNGR_HOST_DIR``, else under ``~/.mngr``."""
    host_dir = environ.get(ENV_MNGR_HOST_DIR, "")
    base = (
        host_dir
        if host_dir
        else os.path.join(os.path.expanduser("~"), _DEFAULT_MNGR_HOST_DIRNAME)
    )
    return os.path.join(base, _AGENT_EVENTS_RELATIVE_PATH)


def _tail_lines_back_to_snapshot(path: str) -> list[str] | None:
    """The file's lines from its last full snapshot on; None when it is absent, unreadable or holds no snapshot."""
    marker = ('"%s"' % _FULL_STATE_EVENT_TYPE).encode("utf-8")
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            tail = b""
            while position > 0:
                step = min(_TAIL_BLOCK_BYTES, position)
                position -= step
                stream.seek(position)
                tail = stream.read(step) + tail
                marker_at = tail.rfind(marker)
                # The snapshot's line is whole once the newline before it (or the file's start) is in hand.
                if marker_at != -1 and (
                    position == 0 or tail.rfind(b"\n", 0, marker_at) != -1
                ):
                    line_start = tail.rfind(b"\n", 0, marker_at) + 1
                    return (
                        tail[line_start:].decode("utf-8", errors="replace").splitlines()
                    )
    except OSError:
        return None
    return None


def _event_time(event: Mapping[str, object]) -> datetime | None:
    """The envelope's timestamp: ISO 8601 with up to nine fractional digits, of which Python keeps six."""
    raw = event.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _listed_agent(details: Mapping[str, object]) -> dict[str, str] | None:
    """One agent's details as the dispatcher's listing gives them; None for details with no id or name."""
    agent_id = details.get("id")
    name = details.get("name")
    if not isinstance(agent_id, str) or not isinstance(name, str) or not name.strip():
        return None
    state = details.get("state")
    labels = as_json_object(details.get("labels")) or {}
    listed = {
        "id": agent_id,
        "name": name.strip(),
        "state": state if isinstance(state, str) else "",
    }
    for label in _LISTED_LABELS:
        value = labels.get(label)
        listed["labels." + label] = (
            ("true" if value else "false")
            if isinstance(value, bool)
            else str(value or "")
        )
    return listed


def fold_agent_events(lines: list[str], now: float) -> list[dict[str, str]] | None:
    """The agents the event lines fold to, or None when they cannot vouch for the workspace's agents."""
    agent_by_id: dict[str, dict[str, str]] = {}
    newest: datetime | None = None
    is_snapshot_seen = False
    for line in lines:
        if not line.strip():
            continue
        try:
            parsed: object = json.loads(line)
        except ValueError:
            continue
        event = as_json_object(parsed)
        if event is None:
            continue
        stamped = _event_time(event)
        if stamped is not None and (newest is None or stamped > newest):
            newest = stamped
        event_type = event.get("type")
        if event_type == _FULL_STATE_EVENT_TYPE:
            is_snapshot_seen = True
            agent_by_id.clear()
            agents = event.get("agents")
            for raw_agent in agents if isinstance(agents, list) else []:
                listed = _listed_agent(as_json_object(raw_agent) or {})
                if listed is not None:
                    agent_by_id[listed["id"]] = listed
        elif event_type in _AGENT_STATE_EVENT_TYPES:
            listed = _listed_agent(as_json_object(event.get("agent")) or {})
            if listed is not None:
                agent_by_id[listed["id"]] = listed
        elif event_type == _AGENT_REMOVED_EVENT_TYPE:
            removed_id = event.get("agent_id")
            if isinstance(removed_id, str):
                agent_by_id.pop(removed_id, None)
        else:
            pass
    if not is_snapshot_seen or newest is None:
        return None
    if datetime.fromtimestamp(now, timezone.utc) - newest > _STALE_AFTER:
        return None
    agents_listed = list(agent_by_id.values())
    # A file without the workspace's own services agent is another host's, and would make every agent read as gone.
    if not any(agent.get("labels.is_primary") == "true" for agent in agents_listed):
        return None
    return agents_listed


def read_observed_agents(events_path: str, now: float) -> list[dict[str, str]] | None:
    """Every agent the observer's file lists, as the dispatcher's listing gives them; None when it cannot say."""
    lines = _tail_lines_back_to_snapshot(events_path)
    if lines is None:
        return None
    return fold_agent_events(lines, now)
