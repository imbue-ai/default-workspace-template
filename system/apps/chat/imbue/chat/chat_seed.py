"""The seed segment: a chat's opening turns written by the Mind app rather than by a harness.

The Mind app's onboarding is a conversation (the "what is honest software" exchange, the
questions about where to run the workspace, the setup and ready lines). Once the workspace
exists that conversation continues inside it as the workspace's first chat, so the app hands
the turns over and the chat app keeps them as the chat's first segment: a file of events in
the wire shape the harness parsers emit, under the chat's folder beside its record, read
through the ``seed`` harness's loader like any archived segment.

The seed's pseudo-agent id is the chat's id (a chat's id is its first agent's), so a seeded
chat's record is well-formed before it has any real agent; the first real agent joins the
record as its second member when the user's first message launches it.
"""

import json
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.primitives import ChatId
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

logger = _loguru_logger

SEED_FILENAME: Final[str] = "seed.jsonl"
# The ``source`` every seed event carries, so a reader can tell a seeded turn from a harness's.
SEED_SOURCE: Final[str] = "seed"
# The pseudo-agent's mngr name, shown on the terminal back face of a chat that has no agent yet.
SEED_AGENT_NAME: Final[str] = "welcome"


class SeedRole(LowerCaseStrEnum):
    """Who said a seeded turn."""

    USER = auto()
    ASSISTANT = auto()


class SeedTurn(FrozenModel):
    """One turn of the conversation the Mind app hands over: who said it, and the markdown they said."""

    role: SeedRole = Field(description="The side of the conversation the turn belongs to")
    text: str = Field(description="The turn's text, as markdown")


@pure
def seed_event_id(chat_id: ChatId, index: int) -> str:
    """Derived, never random, so a re-read of the file dedups (the event-id rule of ``harnesses/events``)."""
    return f"{chat_id}:seed:{index}"


@pure
def seed_events(chat_id: ChatId, turns: tuple[SeedTurn, ...], created_at: datetime) -> list[dict[str, Any]]:
    """The turns as the events the chat pages render, in order, all stamped at ``created_at``.

    The shapes are the core ``user_message`` and ``assistant_message`` contract every harness
    fills (``harnesses/events``), with no tool calls, no thinking, and no payloads to defer.
    """
    timestamp = created_at.astimezone(timezone.utc).isoformat()
    events: list[dict[str, Any]] = []
    for index, turn in enumerate(turns):
        event_id = seed_event_id(chat_id, index)
        match turn.role:
            case SeedRole.USER:
                events.append(
                    {
                        "timestamp": timestamp,
                        "type": "user_message",
                        "event_id": event_id,
                        "source": SEED_SOURCE,
                        "role": "user",
                        "content": turn.text,
                        "message_uuid": event_id,
                        "agent_id": str(chat_id),
                    }
                )
            case SeedRole.ASSISTANT:
                events.append(
                    {
                        "timestamp": timestamp,
                        "type": "assistant_message",
                        "event_id": event_id,
                        "source": SEED_SOURCE,
                        "role": "assistant",
                        "model": "",
                        "text": turn.text,
                        "tool_calls": [],
                        "stop_reason": None,
                        "usage": None,
                        "message_uuid": event_id,
                        "is_auth_error": False,
                        "is_api_error": False,
                        "api_error_kind": None,
                        "is_provider_fault": False,
                        "agent_id": str(chat_id),
                    }
                )
            case _ as unreachable:
                assert_never(unreachable)
    return events


def write_seed_file(chat_dir: Path, events: list[dict[str, Any]]) -> Path:
    """Write the seed segment's events, one JSON object per line, whole (a temp file renamed into place)."""
    chat_dir.mkdir(parents=True, exist_ok=True)
    path = chat_dir / SEED_FILENAME
    temp_path = path.with_suffix(".jsonl.tmp")
    temp_path.write_text("".join(json.dumps(event) + "\n" for event in events))
    temp_path.replace(path)
    return path


def read_seed_events(chat_dir: Path) -> list[dict[str, Any]]:
    """The seed segment's events, in file order; an absent file is an empty segment."""
    path = chat_dir / SEED_FILENAME
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        parsed = json.loads(line)
        if not isinstance(parsed, dict):
            logger.warning(
                "Skipping line {} of the seed file {}: an event is an object, not {}",
                line_number,
                path,
                type(parsed).__name__,
            )
            continue
        events.append(parsed)
    return events


@pure
def seed_agent_info(chat_id: ChatId, chat_dir: Path) -> AgentInfo:
    """The pseudo-agent a seed segment is read as: mngr knows no such agent, so its dirs are the chat's own folder."""
    return AgentInfo(
        id=str(chat_id),
        name=SEED_AGENT_NAME,
        state="STOPPED",
        agent_state_dir=chat_dir,
        claude_config_dir=chat_dir,
        harness=HarnessType.SEED,
    )
