"""A room's transcript: the common transcript its agent keeps, which the chat app appends to and reads.

A room (the multiplayer spec, section 7.3) is an mngr agent of the ``room`` type whose process is a reader of
its own pane (``system/scripts/workspace_bare_scripts/room_agent.py``). Its transcript is one file, the common transcript at
``events/room/common_transcript/events.jsonl`` under the agent's state dir, in the ATIF stream shape ``mngr
transcript`` renders: a header line, then one ``step`` per message, the author under the step's ``extra`` as a
participant key (and the name it was called, for an author the roster does not hold). Two writers append to it:
the chat app, for every message a person or a visiting agent sends through its routes, and the room's reader,
for what ``mngr message`` typed into the pane, which is the workspace agent's. Both append whole lines, so the
file is read like any append-only JSONL stream.

The page reads a room's steps as ``user_message`` events (``step_as_page_event``): every turn of a room is a
participant's, drawn under their name and picture, so the chat page needs no second transcript model for rooms.
The chat app stamps the author of a step that names none from the chat's annotations (``authors.py``), as it
does for every harness's turns; a step that names its author (the reader's) is left as it is.
"""

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

from loguru import logger
from workspace_layout.primitives import WORKSPACE_AGENT_PARTICIPANT_KEY

from imbue.chat.authors import AUTHOR_FIELD
from imbue.chat.authors import AUTHOR_NAME_FIELD
from imbue.chat.authors import split_author_prefix
from imbue.imbue_common.pure import pure
from imbue.mngr.agents.common_transcript_records import PINNED_ATIF_SCHEMA_VERSION

# Pinned beside ``system/scripts/workspace_bare_scripts/room_agent.py``'s copy: the reader writes the same file in the same shape.
TRANSCRIPT_RELATIVE_PATH: Final[Path] = Path("events") / "room" / "common_transcript" / "events.jsonl"
ROOM_EMITTER: Final[str] = "room"
# The ``source`` a room's turn carries on the page.
ROOM_PAGE_EVENT_SOURCE: Final[str] = "room"
# The step's ``extra`` keys: who said it, what they were called, and the composer's id for a person's message.
EXTRA_AUTHOR_KEY: Final[str] = "author"
EXTRA_AUTHOR_NAME_KEY: Final[str] = "author_name"
EXTRA_MESSAGE_ID_KEY: Final[str] = "message_id"


@pure
def transcript_path(agent_state_dir: Path) -> Path:
    return agent_state_dir / TRANSCRIPT_RELATIVE_PATH


@pure
def header_event_id(agent_id: str) -> str:
    """The header's id: a hash of the agent and the emitter, as every common transcript mints it."""
    digest = hashlib.sha256(f"{agent_id}:{ROOM_EMITTER}".encode("utf-8", "replace")).hexdigest()[:32]
    return f"header-{digest}"


@pure
def header_record(agent_id: str) -> dict[str, Any]:
    return {
        "type": "header",
        "event_id": header_event_id(agent_id),
        "emitter": ROOM_EMITTER,
        "schema_version": PINNED_ATIF_SCHEMA_VERSION,
    }


def person_step_record(text: str, message_id: str) -> dict[str, Any]:
    """A message a person sent through the chat app: a ``user`` step carrying the composer's id, its author left to
    the chat's annotations (absent means the owner, as for every chat)."""
    return {
        "type": "step",
        "event_id": f"room-{uuid4().hex}",
        "emitter": ROOM_EMITTER,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "user",
        "message": text,
        "extra": {EXTRA_MESSAGE_ID_KEY: message_id},
    }


def append_record(path: Path, agent_id: str, record: dict[str, Any]) -> None:
    """Append one line, writing the header first when the file is new or empty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as transcript:
        if transcript.tell() == 0:
            transcript.write(json.dumps(header_record(agent_id), ensure_ascii=False) + "\n")
        transcript.write(json.dumps(record, ensure_ascii=False) + "\n")


@pure
def step_as_page_event(record: dict[str, Any]) -> dict[str, Any] | None:
    """A transcript line as the chat page reads it: a ``user_message`` for a step, None for the header or an
    unreadable line (logged)."""
    if record.get("type") != "step":
        return None
    event_id = record.get("event_id")
    message = record.get("message")
    if not isinstance(event_id, str) or not isinstance(message, str):
        logger.warning("Skipped a room transcript step with no id or message: {!r}", record)
        return None
    extra = record.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    event: dict[str, Any] = {
        "timestamp": str(record.get("timestamp", "")),
        "type": "user_message",
        "event_id": event_id,
        "source": ROOM_PAGE_EVENT_SOURCE,
        "role": "user",
        "content": message,
        "message_uuid": event_id,
    }
    # A room learns the author from the chat's annotations, never from a prefix, so a step whose message still
    # leads with one is shown without it; the content stays as written, since the annotations are joined to it by
    # its hash.
    _author_name, spoken = split_author_prefix(message)
    if spoken != message:
        event["display_body"] = spoken
    message_id = extra.get(EXTRA_MESSAGE_ID_KEY)
    if isinstance(message_id, str):
        event["message_id"] = message_id
    author = extra.get(EXTRA_AUTHOR_KEY)
    if isinstance(author, str) and author:
        event[AUTHOR_FIELD] = author
        author_name = extra.get(EXTRA_AUTHOR_NAME_KEY)
        if isinstance(author_name, str):
            event[AUTHOR_NAME_FIELD] = author_name
    return event


@pure
def is_workspace_agent_turn(event: dict[str, Any]) -> bool:
    """Whether a page event is the workspace agent's own post into the room (the answer to a mention)."""
    return event.get("type") == "user_message" and event.get(AUTHOR_FIELD) == str(WORKSPACE_AGENT_PARTICIPANT_KEY)


@pure
def render_transcript(events: Sequence[dict[str, Any]]) -> str:
    """The room's messages as text, one ``[time] name: text`` line each, for the summary a chat leaving the room
    takes with it (the multiplayer spec, section 7.6)."""
    lines: list[str] = []
    for event in events:
        if event.get("type") != "user_message":
            continue
        name = event.get(AUTHOR_NAME_FIELD) or _name_of_key(event.get(AUTHOR_FIELD))
        text = str(event.get("content", "")).replace("\n", "\n    ")
        lines.append(f"[{event.get('timestamp', '')}] {name}: {text}")
    return "\n".join(lines)


@pure
def _name_of_key(author: object) -> str:
    """A fallback name for an author with no recorded name: the owner for none, the key otherwise."""
    if not isinstance(author, str) or not author:
        return "the owner"
    if author == str(WORKSPACE_AGENT_PARTICIPANT_KEY):
        return "the workspace agent"
    return author
