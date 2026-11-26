"""The seed segment: a chat's opening turns written before any agent joined it, rather than by a harness.

The Imbue Studio app's onboarding is a conversation (the "what is honest software" exchange, the
questions about where to run the workspace, the setup and ready lines). Once the workspace
exists that conversation continues inside it as the workspace's first chat, so the app hands
the turns over and the chat app keeps them as the chat's first segment: a file of events in
the wire shape the harness parsers emit, under the chat's folder beside its record, read
through the ``seed`` harness's loader like any archived segment. The chat app seeds one chat
of its own the same way: the main chat it opens for a workspace that had chats before there was
a main chat (``main_chat.py``).

The seed's pseudo-agent id is the chat's id (a chat's id is its first agent's), so a seeded
chat's record is well-formed before it has any real agent; the first real agent joins the
record as its second member when the user's first message launches it.

A seed can name a first message for the chat to send as the user's own once that agent launches
(the app sends ``/install-app <url>`` for a workspace created from an app). It is no turn of the
seed and no part of the chat's record: it is a file of its own beside the seed file, read by the
launch of a chat that has its seed and no agent yet (``AgentManager.create_chat``), so the record
keeps the shape every earlier chat app reads.
"""

import json
from collections.abc import Sequence
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
from imbue.chat.first_run_tree import START_DESCRIBE_LABEL
from imbue.chat.first_run_tree import START_FEATURES_LABEL
from imbue.chat.first_run_tree import START_IDEAS_LABEL
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.message_display import SEED_CONTEXT_TAG
from imbue.chat.primitives import ChatId
from imbue.chat.suggestions import ChatSuggestions
from imbue.chat.suggestions import Suggestion
from imbue.chat.suggestions import SuggestionSource
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

logger = _loguru_logger

SEED_FILENAME: Final[str] = "seed.jsonl"
SEED_FIRST_MESSAGE_FILENAME: Final[str] = "seed_first_message.txt"
# The ``source`` every seed event carries, so a reader can tell a seeded turn from a harness's.
SEED_SOURCE: Final[str] = "seed"
# The pseudo-agent's mngr name, shown on the terminal back face of a chat that has no agent yet.
SEED_AGENT_NAME: Final[str] = "welcome"

# How much of the seeded conversation the launch message carries inline. Past it the agent is
# pointed at the seed file instead, the way a long handoff summary is, and for the same
# delivery limits (measured at ``chat_handoffs.INLINE_SUMMARY_MAX_BYTES``).
INLINE_SEED_MAX_BYTES: Final[int] = 64 * 1024
# The file that marks a chat as the one a first-run workspace opened on. It holds what the user told Imbue
# Studio's first run about themselves (nothing, from an app that does not ask), read only into the first send.
FIRST_RUN_CONTEXT_FILENAME: Final[str] = "first_run_context.txt"
# The skill a first-run chat's first agent is told to follow: it holds the reply to each suggestion of the tree.
FIRST_RUN_SKILL_PATH: Final[str] = ".agents/skills/first-run-onboarding/SKILL.md"


class SeedRole(LowerCaseStrEnum):
    """Who said a seeded turn."""

    USER = auto()
    ASSISTANT = auto()


class SeedTurn(FrozenModel):
    """One turn of the conversation the Imbue Studio app hands over: who said it, and the markdown they said."""

    role: SeedRole = Field(description="The side of the conversation the turn belongs to")
    text: str = Field(description="The turn's text, as markdown")


# The Main chat a first-run workspace opens on (specs/chat-suggestions in mngr): the agent introducing itself and
# Studio, then asking how the user wants to start, over the start suggestions.
FIRST_RUN_WELCOME: Final[tuple[SeedTurn, ...]] = (
    SeedTurn(role=SeedRole.ASSISTANT, text="Hi, I'm your Studio agent. I'll be with you from here on."),
    SeedTurn(role=SeedRole.ASSISTANT, text="You can chat with me any time by clicking my avatar on the desktop."),
    SeedTurn(
        role=SeedRole.ASSISTANT,
        text="With Studio you can:\n\n- Build apps\n- Run agents\n- Automate workflows\n- Consolidate your data",
    ),
    SeedTurn(role=SeedRole.ASSISTANT, text="**How do you want to start?**"),
)


@pure
def first_run_start_suggestions(chat_id: ChatId, turns: tuple[SeedTurn, ...]) -> ChatSuggestions:
    """The suggestions a first-run chat opens with, offered under the last of the ``turns`` it was seeded on."""
    return ChatSuggestions(
        after_event_id=seed_event_id(chat_id, len(turns) - 1),
        source=SuggestionSource.FIXED,
        suggestions=(
            Suggestion(suggestion_id="start-describe", label=START_DESCRIBE_LABEL),
            Suggestion(suggestion_id="start-ideas", label=START_IDEAS_LABEL),
            Suggestion(suggestion_id="start-features", label=START_FEATURES_LABEL),
        ),
    )


def write_first_run_context(chat_dir: Path, context: str) -> None:
    """Mark the chat as a first-run workspace's, keeping what the user told the first run beside the seed (it
    may be nothing) for the chat's first agent."""
    chat_dir.mkdir(parents=True, exist_ok=True)
    (chat_dir / FIRST_RUN_CONTEXT_FILENAME).write_text(context.strip())


def is_first_run_seed(chat_dir: Path) -> bool:
    """Whether the chat is the one a first-run workspace opened on."""
    return (chat_dir / FIRST_RUN_CONTEXT_FILENAME).is_file()


def read_first_run_context(chat_dir: Path) -> str:
    path = chat_dir / FIRST_RUN_CONTEXT_FILENAME
    return path.read_text().strip() if path.is_file() else ""


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
    """The seed segment's events, in file order; an absent file is an empty segment.

    A line that is not a JSON object, or an object with no ``event_id`` (the key every reader
    indexes the segment by), is skipped with a warning rather than failing the whole segment:
    the file is written whole, so a damaged line is outside interference, and the rest of the
    conversation still reads.
    """
    path = chat_dir / SEED_FILENAME
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError as e:
            logger.warning("Skipping line {} of the seed file {}: not valid JSON: {}", line_number, path, e)
            continue
        if not isinstance(parsed, dict):
            logger.warning(
                "Skipping line {} of the seed file {}: an event is an object, not {}",
                line_number,
                path,
                type(parsed).__name__,
            )
            continue
        if not isinstance(parsed.get("event_id"), str):
            logger.warning("Skipping line {} of the seed file {}: the event has no event_id", line_number, path)
            continue
        events.append(parsed)
    return events


def write_seed_first_message(chat_dir: Path, message: str) -> Path:
    """Keep the message a seed asked its chat to send first, whole (a temp file renamed into place)."""
    chat_dir.mkdir(parents=True, exist_ok=True)
    path = chat_dir / SEED_FIRST_MESSAGE_FILENAME
    temp_path = path.with_suffix(".txt.tmp")
    temp_path.write_text(message, encoding="utf-8")
    temp_path.replace(path)
    return path


def read_seed_first_message(chat_dir: Path) -> str:
    """The message a seed asked its chat to send first; empty when it named none."""
    path = chat_dir / SEED_FIRST_MESSAGE_FILENAME
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


@pure
def tagged_turns(turns: Sequence[SeedTurn]) -> str:
    """Turns of a conversation as a model is handed them: one tagged block per turn, in order."""
    return "\n".join(f'<turn speaker="{turn.role}">\n{turn.text.strip()}\n</turn>' for turn in turns)


@pure
def seed_transcript(events: list[dict[str, Any]]) -> str:
    """The seed segment's turns as the conversation they were: one tagged block per turn, in order.

    The turns are markdown written before any agent joined the chat, so they are quoted whole rather than summarised.
    A closing context tag inside a turn is broken up, so the block the page strips always ends
    where this builder put its end.
    """
    turns: list[SeedTurn] = []
    for event in events:
        role = SeedRole.USER if event.get("type") == "user_message" else SeedRole.ASSISTANT
        text = event.get("content") if role is SeedRole.USER else event.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        turns.append(SeedTurn(role=role, text=text))
    return tagged_turns(turns).replace(f"</{SEED_CONTEXT_TAG}>", f"</ {SEED_CONTEXT_TAG}>")


def is_slash_command(message: str) -> bool:
    """Whether a harness would run ``message`` as a command: only when the slash leads it, so anything put
    ahead of such a message (a seed's context block) would turn the command into prose."""
    return message.lstrip().startswith("/")


def seed_context_message(chat_dir: Path, message: str) -> str:
    """A seeded chat's first send as its first agent receives it: the conversation, then the words.

    The seed is a segment this app renders from a file -- no agent ever ran on it, and no
    ``mngr transcript`` can read it -- so the context block is how the conversation reaches the
    chat's first agent at all. The page strips the block and shows the message alone
    (``harnesses/message_display.py``).

    Returns ``message`` unchanged when the chat has no seed to carry (a damaged or absent file),
    which is the behaviour of a chat that never had one, and for a slash command: a harness runs a
    message as a command only when it begins with the slash, so a block ahead of it would turn the
    command into prose.
    """
    if is_slash_command(message):
        return message
    events = read_seed_events(chat_dir)
    transcript = seed_transcript(events)
    if not transcript:
        logger.warning("The seeded chat under {} has no readable turns; its agent starts without them", chat_dir)
        return message
    if len(transcript.encode("utf-8")) > INLINE_SEED_MAX_BYTES:
        # Resolved: the chats root is relative to the app's working directory, and this path is
        # read by another process.
        conversation = (
            f"The conversation is too long to carry in this message. It is on disk at "
            f"{(chat_dir / SEED_FILENAME).resolve()}, one JSON event per line, oldest first. "
            f"Read that file in full before replying."
        )
    else:
        conversation = f"<conversation>\n{transcript}\n</conversation>"
    # CLEANUP: drop this passage, ``read_first_run_context`` and the text ``write_first_run_context`` keeps (the
    # file stays, empty, as the first-run mark) once no supported Imbue Studio app sends ``context`` with its seed.
    first_run_context = read_first_run_context(chat_dir)
    if first_run_context != "":
        safe_context = first_run_context.replace(f"</{SEED_CONTEXT_TAG}>", f"</ {SEED_CONTEXT_TAG}>")
        conversation += (
            "\n\nBefore the workspace existed, the user told Imbue Studio this about what they want it for "
            f"(they did not say it in this chat):\n<about-user>\n{safe_context}\n</about-user>"
        )
    if is_first_run_seed(chat_dir):
        conversation += (
            "\n\nThis is the user's first conversation in a new workspace. Before replying, read "
            f"{FIRST_RUN_SKILL_PATH} and follow it."
        )
    return (
        f"<{SEED_CONTEXT_TAG}>\n"
        "This chat did not begin with you. It opened on the conversation this block carries, "
        "written before any agent joined it, and the chat shows it above your first turn; you are "
        "the first agent to join it. Treat it as the conversation so far: what follows this block is the "
        "user's next turn in it and may lean on it entirely (a bare number, for instance, picks "
        "one of the options the last turn offered). Answer that turn, not this block.\n\n"
        f"{conversation}\n"
        f"</{SEED_CONTEXT_TAG}>\n"
        f"{message}"
    )


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
