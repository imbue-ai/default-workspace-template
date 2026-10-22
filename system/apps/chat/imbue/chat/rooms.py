"""Rooms (the multiplayer spec, section 7.3): a second kind of chat, owned entirely by this app, with a message log
and no agent.

A room is ``data/.apps/chat/rooms/<room_id>/``: ``room.json`` (what the room is) and ``messages.jsonl``, one line
per message under the standard event envelope, carrying who said it (their participant key and the name they were
called at the time) and whom the message mentioned. Everyone in the roster may read and post in every room; there
is no membership. A room's id is a ``ChatId`` like any chat's, minted ``room-<16 hex>``; an awaiting chat turned into
a room keeps the agent-shaped id its page already opened on, which is why the kind is what this store answers and
never the id's prefix.

The page reads a room's messages as ``user_message`` events (``room_message_as_page_event``): every turn of a room
is a person's, drawn under their name and picture like any other person's turn, so the chat page needs no second
transcript model for rooms.
"""

import json
import secrets
import threading
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError
from workspace_layout.primitives import ParticipantKey

from imbue.chat.config import DEFAULT_CHAT_DATA_DIR
from imbue.chat.primitives import ChatId
from imbue.chat.primitives import ChatKind
from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.event_envelope import parse_iso_timestamp
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

ROOMS_DIRNAME: Final[str] = "rooms"
DEFAULT_ROOMS_ROOT: Final[Path] = DEFAULT_CHAT_DATA_DIR / ROOMS_DIRNAME
ROOM_RECORD_FILENAME: Final[str] = "room.json"
ROOM_MESSAGES_FILENAME: Final[str] = "messages.jsonl"
# Bumped when the on-disk shape changes; a room from a newer build is skipped rather than read wrong.
ROOM_RECORD_VERSION: Final[int] = 1
ROOM_MESSAGE_EVENT_TYPE: Final[EventType] = EventType("room_message")
ROOM_MESSAGES_EVENT_SOURCE: Final[EventSource] = EventSource("room_messages")
# The source a room's turn carries on the page, where every turn is a ``user_message`` of the chat it is read in.
ROOM_PAGE_EVENT_SOURCE: Final[str] = "room"
# The room the chat app makes the first time anything needs it (section 7.5).
LOUNGE_TITLE: Final[str] = "Lounge"
# What a room minted with no title is called: the first free "Room N".
ROOM_TITLE_STEM: Final[str] = "Room"


class BlankRoomTitleError(ValueError):
    """A room was given a title with nothing in it."""


class RoomRecord(FrozenModel):
    """What a room is: ``room.json``."""

    version: int = Field(default=ROOM_RECORD_VERSION, description="The on-disk shape this record was written with")
    id: ChatId = Field(description="The room's chat id")
    title: str = Field(description="The name the room is listed and mentioned by")
    created_at: IsoTimestamp = Field(description="When the room was made")
    created_by: ParticipantKey = Field(description="Who made it")


class RoomMessage(EventEnvelope):
    """One line of a room's ``messages.jsonl``."""

    message_id: str = Field(description="The id the sender minted for the message")
    author: ParticipantKey = Field(description="Who said it")
    author_name: str = Field(description="The name the author was called when they said it")
    text: str = Field(description="What they said, verbatim, mentions included")
    mentions: tuple[str, ...] = Field(
        default=(), description="The participant keys and chat ids the message mentioned, in order of appearance"
    )


class RoomSnapshot(FrozenModel):
    """A room as the chat pages list it: what ``GET /api/rooms`` and the ``rooms_updated`` push carry."""

    id: ChatId = Field(description="The room's chat id")
    kind: ChatKind = Field(default=ChatKind.ROOM, description="Always ``room``; the list tells the kinds apart by it")
    title: str = Field(description="The room's name")
    last_messaged_at: float | None = Field(
        description="Epoch seconds of the room's most recent message, or None while it has none"
    )
    awaiting_chat_ids: tuple[ChatId, ...] = Field(
        default=(),
        description="The agent chats a mention from this room sent to, until one answers into it: the room's row "
        "shows a working dot while one of them works",
    )


@pure
def mint_room_id() -> ChatId:
    return ChatId(f"room-{secrets.token_hex(8)}")


@pure
def now_timestamp() -> IsoTimestamp:
    return IsoTimestamp(format_nanosecond_iso_timestamp(datetime.now(timezone.utc)))


def new_room_message(
    message_id: str, author: ParticipantKey, author_name: str, text: str, mentions: Sequence[str] = ()
) -> RoomMessage:
    return RoomMessage(
        timestamp=now_timestamp(),
        type=ROOM_MESSAGE_EVENT_TYPE,
        event_id=EventId(generate_log_event_id()),
        source=ROOM_MESSAGES_EVENT_SOURCE,
        message_id=message_id,
        author=author,
        author_name=author_name,
        text=text,
        mentions=tuple(mentions),
    )


@pure
def room_message_as_page_event(message: RoomMessage) -> dict[str, Any]:
    """The message as the chat page reads it: a ``user_message`` event stamped with its author (section 5.4), so the
    page draws it as that person's turn. ``message_id`` lets the page's own optimistic bubble stand down for it."""
    return {
        "timestamp": str(message.timestamp),
        "type": "user_message",
        "event_id": str(message.event_id),
        "source": ROOM_PAGE_EVENT_SOURCE,
        "role": "user",
        "content": message.text,
        "message_id": message.message_id,
        "author": str(message.author),
        "author_name": message.author_name,
        "mentions": list(message.mentions),
    }


@pure
def _moment_of(timestamp: IsoTimestamp) -> float | None:
    parsed = parse_iso_timestamp(str(timestamp))
    return parsed.timestamp() if parsed is not None else None


class _CachedMessages(MutableModel):
    """One room's messages as last read, and the file stamp they were read at."""

    model_config = {"extra": "forbid", "frozen": False}

    stamp: tuple[int, int] | None = Field(description="The file's (mtime_ns, size) at the read; None for no file")
    messages: tuple[RoomMessage, ...] = Field(description="The lines, in file order")


class RoomStore(MutableModel):
    """The rooms under one directory: their records, their message logs, and which agent chats each is waiting on.

    Thread-safe under one lock: rooms are few and their logs short, so every read goes to the files (the messages
    cached by the file's stamp) and every write appends or replaces a file whole.
    """

    model_config = {"extra": "forbid", "frozen": False}

    root: Path = Field(frozen=True, description="The rooms directory (``data/.apps/chat/rooms``)")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _messages_cache: dict[str, _CachedMessages] = PrivateAttr(default_factory=dict)
    # The agent chats each room has mentioned and not heard back from, in memory: a restart forgets them, and the
    # row's dot with them, which is harmless.
    _awaiting_by_room: dict[str, list[ChatId]] = PrivateAttr(default_factory=dict)

    def _room_dir(self, room_id: ChatId) -> Path:
        return self.root / str(room_id)

    def _record_path(self, room_id: ChatId) -> Path:
        return self._room_dir(room_id) / ROOM_RECORD_FILENAME

    def _messages_path(self, room_id: ChatId) -> Path:
        return self._room_dir(room_id) / ROOM_MESSAGES_FILENAME

    def read(self, room_id: ChatId) -> RoomRecord | None:
        """The room, or None for an id that names none (an unreadable record is logged and reads as none)."""
        return _read_record(self._record_path(room_id))

    def read_all(self) -> list[RoomRecord]:
        """Every readable room, oldest first."""
        if not self.root.is_dir():
            return []
        records = [
            record
            for path in sorted(self.root.glob(f"*/{ROOM_RECORD_FILENAME}"))
            if (record := _read_record(path)) is not None
        ]
        return sorted(records, key=lambda record: (str(record.created_at), str(record.id)))

    def find_by_title(self, title: str) -> RoomRecord | None:
        """The room called ``title`` (case-insensitively), the oldest when several are."""
        with self._lock:
            return self._find_by_title_locked(title)

    def _find_by_title_locked(self, title: str) -> RoomRecord | None:
        wanted = title.strip().casefold()
        return next((record for record in self.read_all() if record.title.casefold() == wanted), None)

    def create(self, title: str, created_by: ParticipantKey, room_id: ChatId | None = None) -> RoomRecord:
        """Make a room; an empty title takes the first free "Room N". A caller passes ``room_id`` to turn an
        awaiting chat into a room under the id its page already opened on."""
        with self._lock:
            return self._create_locked(title, created_by, room_id)

    def ensure_lounge(self, created_by: ParticipantKey) -> tuple[RoomRecord, bool]:
        """The Lounge (section 7.5), made now if none is called that, and whether this call made it. Found and made
        under one lock, so two callers asking at once get the same room."""
        with self._lock:
            existing = self._find_by_title_locked(LOUNGE_TITLE)
            if existing is not None:
                return existing, False
            return self._create_locked(LOUNGE_TITLE, created_by, None), True

    def rename(self, room_id: ChatId, title: str) -> RoomRecord | None:
        """The room under its new title, or None for no such room; a title that is only whitespace raises."""
        stripped = title.strip()
        if not stripped:
            raise BlankRoomTitleError("A room's title cannot be blank")
        with self._lock:
            record = _read_record(self._record_path(room_id))
            if record is None:
                return None
            renamed = record.model_copy_update(to_update(record.field_ref().title, stripped))
            self._write_record_locked(renamed)
            return renamed

    def delete(self, room_id: ChatId) -> bool:
        """Remove the room and its messages; False for no such room."""
        with self._lock:
            room_dir = self._room_dir(room_id)
            if not (room_dir / ROOM_RECORD_FILENAME).is_file():
                return False
            for name in (ROOM_MESSAGES_FILENAME, ROOM_RECORD_FILENAME):
                (room_dir / name).unlink(missing_ok=True)
            try:
                room_dir.rmdir()
            except OSError as e:
                logger.warning("Removed room {}'s files but not its folder: {}", room_id, e)
            self._messages_cache.pop(str(room_id), None)
            self._awaiting_by_room.pop(str(room_id), None)
        logger.info("Deleted room {}", room_id)
        return True

    def append(self, room_id: ChatId, message: RoomMessage) -> None:
        with self._lock:
            path = self._messages_path(room_id)
            with path.open("a", encoding="utf-8") as messages_file:
                messages_file.write(message.model_dump_json() + "\n")

    def messages(self, room_id: ChatId, after: str | None = None) -> tuple[RoomMessage, ...]:
        """The room's messages in order; with ``after``, only those after the message with that event id (all of
        them when no message has it)."""
        path = self._messages_path(room_id)
        try:
            stat = path.stat()
            stamp: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            stamp = None
        with self._lock:
            cached = self._messages_cache.get(str(room_id))
            if cached is not None and cached.stamp == stamp:
                messages = cached.messages
            else:
                messages = _read_messages(path) if stamp is not None else ()
                self._messages_cache[str(room_id)] = _CachedMessages(stamp=stamp, messages=messages)
        if after is None:
            return messages
        index = next((i for i, message in enumerate(messages) if str(message.event_id) == after), None)
        return messages if index is None else messages[index + 1 :]

    def last_messaged_at(self, room_id: ChatId) -> float | None:
        messages = self.messages(room_id)
        return _moment_of(messages[-1].timestamp) if messages else None

    def note_agent_asked(self, room_id: ChatId, chat_id: ChatId) -> None:
        """A mention from the room went to ``chat_id``; the room waits for its answer."""
        with self._lock:
            waiting = self._awaiting_by_room.setdefault(str(room_id), [])
            if chat_id not in waiting:
                waiting.append(chat_id)

    def note_agent_answered(self, room_id: ChatId) -> None:
        """An agent posted into the room: it waits on none."""
        with self._lock:
            self._awaiting_by_room.pop(str(room_id), None)

    def snapshot(self, record: RoomRecord) -> RoomSnapshot:
        with self._lock:
            awaiting = tuple(self._awaiting_by_room.get(str(record.id), ()))
        return RoomSnapshot(
            id=record.id,
            title=record.title,
            last_messaged_at=self.last_messaged_at(record.id),
            awaiting_chat_ids=awaiting,
        )

    def snapshots(self) -> list[RoomSnapshot]:
        return [self.snapshot(record) for record in self.read_all()]

    def _create_locked(self, title: str, created_by: ParticipantKey, room_id: ChatId | None) -> RoomRecord:
        record = RoomRecord(
            id=room_id if room_id is not None else mint_room_id(),
            title=title.strip() or self._mint_title_locked(),
            created_at=now_timestamp(),
            created_by=created_by,
        )
        self._write_record_locked(record)
        logger.info("Created room {} ({!r})", record.id, record.title)
        return record

    def _mint_title_locked(self) -> str:
        taken = {record.title.casefold() for record in self.read_all()}
        number = 1
        while f"{ROOM_TITLE_STEM} {number}".casefold() in taken:
            number += 1
        return f"{ROOM_TITLE_STEM} {number}"

    def _write_record_locked(self, record: RoomRecord) -> None:
        path = self._record_path(record.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
        temp_path.replace(path)


def _read_record(path: Path) -> RoomRecord | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("Skipped an unreadable room record at {}: {}", path, e)
        return None
    version = raw.get("version", 0) if isinstance(raw, dict) else 0
    if not isinstance(version, int) or version > ROOM_RECORD_VERSION:
        logger.warning("Skipped the room record at {}: version {!r} is newer than this build reads", path, version)
        return None
    try:
        record = RoomRecord.model_validate(raw)
    except ValidationError as e:
        logger.warning("Skipped an unreadable room record at {}: {}", path, e)
        return None
    if str(record.id) != path.parent.name:
        logger.warning("Skipped the room record at {}: it names room {}", path, record.id)
        return None
    return record


def _read_messages(path: Path) -> tuple[RoomMessage, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        logger.warning("Could not read the messages of room {}: {}", path.parent.name, e)
        return ()
    messages: list[RoomMessage] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            messages.append(RoomMessage.model_validate(json.loads(line)))
        except (ValueError, ValidationError) as e:
            logger.warning("Skipped an unreadable message line of room {}: {}", path.parent.name, e)
    return tuple(messages)
