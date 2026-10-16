"""The agent-access audit events: one JSONL line per invite, join, revoke, and expiry.

They land at ``$MNGR_AGENT_STATE_DIR/events/agent_access/events.jsonl`` under
the standard event envelope (``timestamp``, ``type``, ``event_id``,
``source``), beside the workspace's other event logs, so ``mngr event`` and the
backups see them. They are the owner's record of the grants' lifecycle, not an
audit log: a workspace-scope agent holds the workspace's authority and can edit
any file the workspace keeps, so nothing kept here records what agents ran.
"""

import json
import os
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from share_gateway.log import log

EVENT_SOURCE = "agent_access"
EVENTS_FILENAME = "events.jsonl"


def events_dir_from_environ(
    environ: "os._Environ[str] | dict[str, str]",
) -> Path | None:
    state_dir = environ.get("MNGR_AGENT_STATE_DIR", "")
    if not state_dir:
        return None
    return Path(state_dir) / "events" / EVENT_SOURCE


def _nanosecond_timestamp() -> str:
    nanoseconds = time.time_ns()
    seconds, remainder = divmod(nanoseconds, 1_000_000_000)
    base = datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{remainder:09d}Z"


def write_agent_access_event(events_dir: Path | None, event_type: str, fields: dict[str, object]) -> None:
    """Append one event; an unset state dir or a failed write is logged and never fails the caller."""
    if events_dir is None:
        log(f"MNGR_AGENT_STATE_DIR unset; not recording agent-access event {event_type}")
        return
    event: dict[str, object] = {
        "timestamp": _nanosecond_timestamp(),
        "type": event_type,
        "event_id": f"evt-{uuid4().hex}",
        "source": EVENT_SOURCE,
        **fields,
    }
    try:
        events_dir.mkdir(parents=True, exist_ok=True)
        with (events_dir / EVENTS_FILENAME).open("a") as handle:
            handle.write(json.dumps(event, default=str) + "\n")
    except OSError as exc:
        log(f"Could not record agent-access event {event_type}: {exc}")
