#!/usr/bin/env python3
"""SessionStart hook: tell a revived agent it was stopped for memory pressure.

When the memory watchdog sheds an agent's own process (tier 5/7), it records the
kill in the shed ledger. The agent stays down until the user next messages it,
which restarts the claude process and fires this hook. The hook looks for shed
records naming this agent that have not yet been delivered, prints a notice
(SessionStart stdout becomes session context), and appends a delivery marker so
the same notice is not injected again.

Self-contained (stdlib only) so it runs in the agent's plain claude environment
without importing the memory_watchdog package.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# This hook runs under a bare `python3` (see .claude/settings.json), so none of
# memory_watchdog's third-party deps (loguru, pydantic) are importable. The
# ledger *layout*, however, lives in memory_watchdog.paths, which is deliberately
# stdlib-only -- so we put that package's source dir on sys.path and import the
# shared path helper rather than duplicating the layout and risking drift. The
# MEMORY_WATCHDOG_RUNTIME_DIR override and work-dir base are thus honored
# identically by the writer and this reader.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "memory_watchdog" / "src")
)

from memory_watchdog.paths import shed_ledger_path

_RECORD_TYPE_PROCESS_SHED = "process_shed"
_RECORD_TYPE_NOTICE_DELIVERED = "notice_delivered"


def _read_ledger_records(ledger_path: Path) -> list[dict]:
    if not ledger_path.exists():
        return []
    records: list[dict] = []
    for line in ledger_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _latest_delivered_timestamp(records: list[dict], agent_name: str) -> str:
    """Highest up_to_timestamp already delivered to this agent (or empty)."""
    delivered = [
        str(r.get("up_to_timestamp", ""))
        for r in records
        if r.get("type") == _RECORD_TYPE_NOTICE_DELIVERED
        and r.get("agent_name") == agent_name
    ]
    return max(delivered) if delivered else ""


def _pending_shed_timestamps(
    records: list[dict], agent_name: str, after_timestamp: str
) -> list[str]:
    """Timestamps of this agent's own shed records newer than the last delivery."""
    pending: list[str] = []
    for record in records:
        if record.get("type") != _RECORD_TYPE_PROCESS_SHED:
            continue
        if record.get("agent_name") != agent_name:
            continue
        timestamp = str(record.get("timestamp", ""))
        if timestamp and timestamp > after_timestamp:
            pending.append(timestamp)
    return pending


def _append_delivery_marker(
    ledger_path: Path, agent_name: str, up_to_timestamp: str
) -> None:
    marker = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f000Z"),
        "type": _RECORD_TYPE_NOTICE_DELIVERED,
        "agent_name": agent_name,
        "up_to_timestamp": up_to_timestamp,
    }
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a") as ledger_file:
        ledger_file.write(json.dumps(marker) + "\n")


def main() -> None:
    agent_name = os.environ.get("MNGR_AGENT_NAME", "")
    if not agent_name:
        return
    ledger_path = shed_ledger_path()
    records = _read_ledger_records(ledger_path)
    if not records:
        return
    last_delivered = _latest_delivered_timestamp(records, agent_name)
    pending = _pending_shed_timestamps(records, agent_name, last_delivered)
    if not pending:
        return

    print(
        "Note: you were previously stopped to relieve a memory-pressure "
        "(out-of-memory) situation in this workspace. Any background tasks you "
        "had running -- for example polling loops waiting on another agent or an "
        "external event -- were cancelled and were NOT automatically restarted. "
        "If you were in the middle of multi-step work, re-check the current state "
        "before continuing rather than assuming your last action completed."
    )
    _append_delivery_marker(ledger_path, agent_name, max(pending))


if __name__ == "__main__":
    main()
