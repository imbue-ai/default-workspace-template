#!/usr/bin/env python3
"""Record that the workspace container started with its services agent already in place.

The container start hook (``system/scripts/minds_container_start_hook.sh``) runs
this before it relaunches the services agent, so the user has a record of the
restart beside the workspace's other event logs. The hook cannot tell a restart
docker made on its own (an OOM kill, a docker daemon restart, a VM reboot) from
a start of a stopped workspace, so the event names neither as the cause. One
``container_restarted`` line lands at ``<agent dir>/events/host_lifecycle/events.jsonl``
under the standard event envelope. Stdlib only: the hook runs it under the
system ``python3``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO
from uuid import uuid4

EVENT_SOURCE = "host_lifecycle"
EVENT_TYPE = "container_restarted"
UPTIME_PATH = Path("/proc/uptime")


def nanosecond_timestamp(nanoseconds: int) -> str:
    seconds, remainder = divmod(nanoseconds, 1_000_000_000)
    base = datetime.fromtimestamp(seconds, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    return f"{base}.{remainder:09d}Z"


def read_container_uptime_seconds(uptime_path: Path) -> float | None:
    """How long the container has been up, from the sandbox's own /proc/uptime; None when unreadable."""
    try:
        return float(uptime_path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def build_restart_event(
    agent_id: str, uptime_seconds: float | None, nanoseconds: int
) -> dict[str, object]:
    return {
        "timestamp": nanosecond_timestamp(nanoseconds),
        "type": EVENT_TYPE,
        "event_id": f"evt-{uuid4().hex}",
        "source": EVENT_SOURCE,
        "agent_id": agent_id,
        "container_uptime_seconds": uptime_seconds,
        "message": (
            "The workspace container started with its services down (a restart after an "
            "out-of-memory kill, a docker daemon restart or a VM reboot, or a start of a stopped "
            "workspace) and its services are being relaunched."
        ),
    }


def record_container_restart(
    agent_dir: Path, uptime_path: Path, nanoseconds: int
) -> Path:
    """Append the event to the agent's host_lifecycle stream and return the file written."""
    events_path = agent_dir / "events" / EVENT_SOURCE / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event = build_restart_event(
        agent_dir.name, read_container_uptime_seconds(uptime_path), nanoseconds
    )
    with events_path.open("a") as handle:
        handle.write(json.dumps(event) + "\n")
    return events_path


def main(argv: list[str], stdout: TextIO, stderr: TextIO) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--agent-dir",
        required=True,
        type=Path,
        help="The services agent's state directory",
    )
    args = parser.parse_args(argv)
    if not args.agent_dir.is_dir():
        print(f"not an agent directory: {args.agent_dir}", file=stderr)
        return 2
    events_path = record_container_restart(args.agent_dir, UPTIME_PATH, time.time_ns())
    print(f"recorded {EVENT_TYPE} in {events_path}", file=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdout, sys.stderr))
