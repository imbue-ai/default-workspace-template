#!/usr/bin/env python3
"""The process of a chat room's agent (the multiplayer spec, section 7.3): the room's reader.

A room is an mngr agent of the ``room`` type (``[agent_types.room]`` in ``.mngr/settings.toml``), and this is
what runs in its pane. It reads what ``mngr message <room>`` types there, one line (or one bracketed paste) per
message, and appends each as a step of the room's transcript, the common transcript at
``$MNGR_AGENT_STATE_DIR/events/room/common_transcript/events.jsonl`` that ``mngr transcript <room>`` renders and
the chat app tails. People's messages reach the same file through the chat app, which appends them itself with
the sender's identity; what arrives here was typed by a process in the workspace, so it is the workspace agent's
unless it leads with the ``[from: <name>] `` prefix the agents' own messages carry, which names its author.

Each message also records agent activity for mngr's idle detection. Standard library only: it runs under the
pane's bare ``python3``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

# The transcript's place under the agent's state dir, and the record shape the chat app's room harness reads
# (``system/apps/chat/imbue/chat/harnesses/room/transcript.py`` pins the same constants).
TRANSCRIPT_RELATIVE_PATH = (
    Path("events") / "room" / "common_transcript" / "events.jsonl"
)
ACTIVITY_RELATIVE_PATH = Path("activity") / "agent"
EMITTER = "room"
SCHEMA_VERSION = "ATIF-v1.7"
WORKSPACE_AGENT_AUTHOR = "workspace-agent"
NAME_AUTHOR_PREFIX = "name:"

# ``[from: Name] `` at the start of a typed line: the author convention of ``AGENTS.md``.
AUTHOR_PREFIX_PATTERN = re.compile(r"^\[from: (?P<name>[^\]\n\s][^\]\n]*?)\] ")

# The bracketed-paste markers tmux wraps a long message in (``mngr message`` pastes past 1024 characters), so
# such a message arrives as one unit rather than one message per line.
BRACKETED_PASTE_ENABLE = b"\x1b[?2004h"
BRACKETED_PASTE_DISABLE = b"\x1b[?2004l"
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"


def header_event_id(agent_id: str) -> str:
    """The header's id: a hash of the agent and the emitter, as every common transcript mints it."""
    digest = hashlib.sha256(
        f"{agent_id}:{EMITTER}".encode("utf-8", "replace")
    ).hexdigest()[:32]
    return f"header-{digest}"


def header_record(agent_id: str) -> dict[str, Any]:
    return {
        "type": "header",
        "event_id": header_event_id(agent_id),
        "emitter": EMITTER,
        "schema_version": SCHEMA_VERSION,
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def step_record(
    text: str, author: str, author_name: str | None, timestamp: str
) -> dict[str, Any]:
    """One message as a step: a person's (``source`` user) or the workspace agent's (``source`` agent), the author
    under ``extra`` as the chat app reads it."""
    extra: dict[str, Any] = {"author": author}
    if author_name is not None:
        extra["author_name"] = author_name
    return {
        "type": "step",
        "event_id": f"room-{uuid.uuid4().hex}",
        "emitter": EMITTER,
        "timestamp": timestamp,
        "source": "agent" if author == WORKSPACE_AGENT_AUTHOR else "user",
        "message": text,
        "extra": extra,
    }


def author_of_typed_text(text: str) -> tuple[str, str | None, str]:
    """Who a typed message is from and what they said: the name a ``[from: ]`` prefix carries (as a synthetic key,
    since nothing here can resolve it to a participant), else the workspace agent."""
    match = AUTHOR_PREFIX_PATTERN.match(text)
    if match is None:
        return WORKSPACE_AGENT_AUTHOR, None, text
    name = match.group("name")
    return f"{NAME_AUTHOR_PREFIX}{name}", name, text[match.end() :]


def append_record(transcript_path: Path, record: dict[str, Any]) -> None:
    """Append one line; the file's header is written first when the file is new or empty."""
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    with transcript_path.open("a", encoding="utf-8") as transcript:
        transcript.write(json.dumps(record, ensure_ascii=False) + "\n")


def ensure_header(transcript_path: Path, agent_id: str) -> None:
    if transcript_path.is_file() and transcript_path.stat().st_size > 0:
        return
    append_record(transcript_path, header_record(agent_id))


def record_activity(state_dir: Path, agent_id: str) -> None:
    """Touch mngr's agent-activity file, so a busy room keeps its host awake like any agent."""
    activity_path = state_dir / ACTIVITY_RELATIVE_PATH
    activity_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    activity_path.write_text(
        json.dumps(
            {"time": int(now.timestamp() * 1000), "agent_id": agent_id}, indent=2
        )
    )


def read_messages(stdin: IO[bytes]) -> Iterator[str]:
    """Yield each typed message: a line, or the whole of a bracketed paste, without its markers or trailing newline."""
    pasted: list[bytes] | None = None
    while True:
        line = stdin.readline()
        if not line:
            return
        if pasted is not None:
            end = line.find(PASTE_END)
            if end == -1:
                pasted.append(line)
                continue
            pasted.append(line[:end])
            yield b"".join(pasted).decode("utf-8", errors="replace").rstrip("\r\n")
            pasted = None
            continue
        start = line.find(PASTE_START)
        if start != -1:
            rest = line[start + len(PASTE_START) :]
            end = rest.find(PASTE_END)
            if end == -1:
                pasted = [rest]
                continue
            yield rest[:end].decode("utf-8", errors="replace").rstrip("\r\n")
            continue
        yield line.decode("utf-8", errors="replace").rstrip("\r\n")


def state_dir_of(environ: dict[str, str]) -> Path:
    """The agent's state dir, as mngr names it to the agent (``MNGR_AGENT_STATE_DIR``), else by convention."""
    explicit = environ.get("MNGR_AGENT_STATE_DIR", "").strip()
    if explicit:
        return Path(explicit)
    host_dir = environ.get("MNGR_HOST_DIR", "").strip() or str(Path.home() / ".mngr")
    return Path(host_dir) / "agents" / environ.get("MNGR_AGENT_ID", "")


def main(environ: dict[str, str], stdin: IO[bytes], stdout: IO[bytes]) -> int:
    agent_id = environ.get("MNGR_AGENT_ID", "").strip()
    if not agent_id:
        print(
            "room_agent: MNGR_AGENT_ID is not set; this runs only as an mngr agent",
            file=sys.stderr,
        )
        return 1
    state_dir = state_dir_of(environ)
    transcript_path = state_dir / TRANSCRIPT_RELATIVE_PATH
    ensure_header(transcript_path, agent_id)
    stdout.write(BRACKETED_PASTE_ENABLE)
    stdout.flush()
    try:
        for text in read_messages(stdin):
            if not text.strip():
                continue
            author, author_name, said = author_of_typed_text(text)
            append_record(
                transcript_path, step_record(said, author, author_name, now_iso())
            )
            record_activity(state_dir, agent_id)
    finally:
        stdout.write(BRACKETED_PASTE_DISABLE)
        stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(dict(os.environ), sys.stdin.buffer, sys.stdout.buffer))
