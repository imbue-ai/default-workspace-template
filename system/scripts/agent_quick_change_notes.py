#!/usr/bin/env python3
"""The reading half of agent_quick_change_notes.sh: pass a chat's new quick-change hand-overs and reports on to its agent.

Reads the hook payload on stdin (only for the event name), the quick-change log, and this chat's
state file. Prints one hookSpecificOutput.additionalContext JSON object with one line per hand-over
or report the agent has not been told about, or nothing. The state file is plain text: the log size
when it was last read, then one key per note already passed on.

Standard library only: the hook runs it with the system ``python3``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
from pathlib import Path
from typing import Any

# The events this hook is registered for; any other name gets no note, since a harness only takes
# additionalContext back under the event it sent.
EVENTS = ("PreToolUse", "UserPromptSubmit")
NOTE_SUFFIX = "Do not mention this to the user unless they ask."
# Enough of the user's message for the agent to know which change it is.
ROUTED_MESSAGE_LIMIT = 200


def read_records(log_path: Path) -> list[dict[str, Any]]:
    """Every well-formed JSON object line in the log; a line cut short by a concurrent append is skipped."""
    records: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def report_key(record: dict[str, Any]) -> str:
    """A report's identity: a request can be reported more than once (a retry after a failure)."""
    return f"{record.get('request_id')} {record.get('reported_at')}"


def routed_key(record: dict[str, Any]) -> str:
    """A hand-over's identity, kept apart from report keys (which start with the request id)."""
    return f"routed {record.get('request_id')}"


def routed_note_for(request: dict[str, Any]) -> str:
    message = " ".join(str(request.get("message", "")).split())
    if len(message) > ROUTED_MESSAGE_LIMIT:
        message = message[: ROUTED_MESSAGE_LIMIT - 3].rstrip() + "..."
    return (
        f"Background: the user's message {json.dumps(message, ensure_ascii=False)} was routed to the "
        "quick-change agent, which is making that change now. Do not make it yourself, and avoid "
        "editing that part of the app until a follow-up note says it is done. "
        f"{NOTE_SUFFIX}"
    )


def note_for(report: dict[str, Any], app: str | None) -> str:
    where = f"in the {app} app" if app else "in an app"
    summary = " ".join(str(report.get("report", "")).split())
    if not summary.endswith((".", "!", "?")):
        summary += "."
    if report.get("status") == "done":
        return (
            f"Background: the quick-change agent finished a change {where}: {summary} "
            f"The live app files already include it; keep it when you edit them. {NOTE_SUFFIX}"
        )
    return f"Background: the quick-change agent could not finish a change {where}: {summary} {NOTE_SUFFIX}"


def new_notes(
    records: list[dict[str, Any]], chat_id: str, passed_on: set[str]
) -> tuple[list[str], list[str]]:
    """The notes for this chat's hand-overs and reports not yet passed on, in log order, and their keys.

    A hand-over is the "forward" line with status "forwarded": the "request" line is written before
    the hand-over is tried, and a failed one ("forward_failed") leaves the message with this agent.
    """
    request_by_id = {
        record.get("request_id"): record
        for record in records
        if record.get("type") == "request" and record.get("chat_id") == chat_id
    }
    notes: list[str] = []
    keys: list[str] = []
    for record in records:
        request = request_by_id.get(record.get("request_id"))
        if request is None:
            continue
        if record.get("type") == "forward" and record.get("status") == "forwarded":
            key, note = routed_key(record), routed_note_for(request)
        elif record.get("type") == "report":
            key, note = report_key(record), note_for(record, request.get("app"))
        else:
            continue
        if key in passed_on:
            continue
        notes.append(note)
        keys.append(key)
    return notes, keys


def read_state(state_path: Path) -> set[str]:
    try:
        lines = state_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    return {line for line in lines[1:] if line}


def write_state(state_path: Path, log_size: int, passed_on: set[str]) -> None:
    temp_path = state_path.with_name(state_path.name + ".tmp")
    temp_path.write_text(
        "\n".join([str(log_size), *sorted(passed_on)]) + "\n", encoding="utf-8"
    )
    temp_path.replace(state_path)


def run(chat_id: str, log_path: Path, state_path: Path, event: str) -> str | None:
    """The additionalContext to emit, or None; records what it passes on before returning it."""
    if event not in EVENTS:
        return None
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # Parallel tool calls run this hook concurrently; the lock keeps them from passing one report on twice.
    with open(state_path.with_name(state_path.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # The size before the read, so a report appended during the read changes the size again.
        log_size = log_path.stat().st_size
        passed_on = read_state(state_path)
        notes, keys = new_notes(read_records(log_path), chat_id, passed_on)
        write_state(state_path, log_size, passed_on | set(keys))
    return "\n".join(notes) if notes else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        event = payload.get("hook_event_name", "") if isinstance(payload, dict) else ""
        context = run(args.chat_id, args.log, args.state, event)
    except (OSError, ValueError):
        return 0
    if context is not None:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": event,
                        "additionalContext": context,
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
