"""Who wrote each message (the multiplayer spec, section 5): the sidecar annotation the send route writes for a
message from someone other than the owner, and how it is joined to the transcript turn it became.

A message reaches the agent through its pane, so the transcript knows nothing of its author; the chat app keeps,
per chat, an append-only ``authors.jsonl`` under the standard event envelope, one line per send a visitor or a
visiting agent makes, keyed by the composer's ``message_id`` and carrying the hash of the delivered text. The line
is written before the text is delivered, so the turn's first stamp (which may run before the send route returns)
already sees it; a send the chat then refuses leaves a line that matches no turn.
A message with no annotation is the owner's, as is every message from before the convention. The agent learns a
non-owner author from the ``[from: <name>] `` prefix the send route puts on the delivered text (never on a slash
command or bash-mode input, which a harness runs only when it leads the message).

A ``user_message`` event is stamped with ``author`` (the key) and ``author_name`` by the first of: the annotation
whose text hash is the hash of the turn's content, nearest in time (which keeps a visitor's unprefixed slash
command its author); the annotation whose display name the turn's prefix names, nearest in time, else a synthetic
``name:<name>`` key for a prefix typed by hand; nothing, which is the owner. The stamping is stateless over the
chat's annotations, so a restart, a reload, or an archived segment all read the same way.
"""

import hashlib
import json
import re
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

from imbue.chat.primitives import ChatId
from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

AUTHORS_FILENAME: Final[str] = "authors.jsonl"
AUTHORS_EVENT_SOURCE: Final[EventSource] = EventSource("authors")
MESSAGE_AUTHOR_EVENT_TYPE: Final[EventType] = EventType("message_author")

# The fields stamped on a ``user_message`` event (the wire names the chat page reads).
AUTHOR_FIELD: Final[str] = "author"
AUTHOR_NAME_FIELD: Final[str] = "author_name"

# The prefix the agent reads an author from: ``[from: Name] `` or ``[from: Name in #Room title] ``, at the start of
# a line. The room form is for a mention delivered from a room (section 7.4); either way the name is the author.
AUTHOR_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\[from: (?P<name>[^\]\n\s][^\]\n]*?)(?: in #(?P<room>[^\]\n]+?))?\] "
)

# A name inside the prefix may not hold the bracket that closes it, and the prefix is one line.
_WHITESPACE_RUN: Final[re.Pattern[str]] = re.compile(r"\s+")
# The key of an author known only from a prefix nobody annotated (typed by hand, or whose annotation was lost).
NAME_KEY_PREFIX: Final[str] = "name:"
# A name a prefix carries may hold anything but the bracket; the key form holds it to one line of at most this.
_MAX_NAME_KEY_LENGTH: Final[int] = 60


class AuthorAnnotation(EventEnvelope):
    """One line of a chat's ``authors.jsonl``: who sent the message the composer id names."""

    message_id: str = Field(description="The composer-minted id of the send")
    author: ParticipantKey = Field(description="The sender's participant key")
    display_name: str = Field(description="The sender's name as resolved at send time")
    text_sha256: str = Field(description="The SHA-256 of the text as delivered to the agent (prefix included)")


@pure
def author_prefix(display_name: str, room_title: str | None = None) -> str:
    """The prefix the delivered text carries for a non-owner author (section 5.3)."""
    if room_title is None:
        return f"[from: {display_name}] "
    return f"[from: {display_name} in #{room_title}] "


@pure
def prefix_safe_name(display_name: str, key: ParticipantKey) -> str:
    """The name as the prefix can carry it (and as the annotation records it, so a prefix joins by name): the
    closing bracket dropped, whitespace runs (line breaks included) as one space; the key when nothing is left."""
    cleaned = _WHITESPACE_RUN.sub(" ", display_name.replace("]", "")).strip()
    return cleaned if cleaned else str(key)


@pure
def is_unprefixable_input(text: str) -> bool:
    """Whether a text is a slash command or bash-mode input, which a harness runs only when it leads the message, so
    it is never prefixed."""
    return text.lstrip().startswith(("/", "!"))


@pure
def split_author_prefix(text: str) -> tuple[str | None, str]:
    """The prefix at the start of ``text`` (or of any of its lines, for a turn a harness merged from several queued
    messages), and the text with that prefix taken off: ``(name, rest)``, or ``(None, text)`` without one."""
    match = AUTHOR_PREFIX_PATTERN.match(text)
    if match is not None:
        return match.group("name"), text[match.end() :]
    lines = text.split("\n")
    for index, line in enumerate(lines):
        line_match = AUTHOR_PREFIX_PATTERN.match(line)
        if line_match is not None:
            stripped = [*lines[:index], line[line_match.end() :], *lines[index + 1 :]]
            return line_match.group("name"), "\n".join(stripped)
    return None, text


@pure
def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pure
def name_key(display_name: str) -> ParticipantKey:
    """The synthetic key of an author known only by the name a prefix carries."""
    return ParticipantKey(f"{NAME_KEY_PREFIX}{display_name.strip()[:_MAX_NAME_KEY_LENGTH]}")


def new_author_annotation(
    message_id: str, author: ParticipantKey, display_name: str, delivered_text: str
) -> AuthorAnnotation:
    return AuthorAnnotation(
        timestamp=IsoTimestamp(format_nanosecond_iso_timestamp(datetime.now(timezone.utc))),
        type=MESSAGE_AUTHOR_EVENT_TYPE,
        event_id=EventId(generate_log_event_id()),
        source=AUTHORS_EVENT_SOURCE,
        message_id=message_id,
        author=author,
        display_name=display_name,
        text_sha256=text_sha256(delivered_text),
    )


# fromisoformat reads at most six fraction digits; a nanosecond timestamp is cut to microseconds.
_LONG_FRACTION: Final[re.Pattern[str]] = re.compile(r"\.(\d{6})\d+")


@pure
def _moment_of(timestamp: str) -> float | None:
    """Seconds since the epoch of an ISO 8601 timestamp of any fraction length; None when it does not read."""
    trimmed = _LONG_FRACTION.sub(r".\1", timestamp.strip())
    if trimmed.endswith("Z"):
        trimmed = trimmed[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(trimmed)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


@pure
def _nearest(candidates: Sequence[AuthorAnnotation], event_timestamp: str) -> AuthorAnnotation | None:
    """The candidate nearest in time to the event; the last one when neither side's time reads."""
    if not candidates:
        return None
    event_moment = _moment_of(event_timestamp)
    if event_moment is None:
        return candidates[-1]
    timed = [
        (abs(moment - event_moment), index)
        for index, candidate in enumerate(candidates)
        if (moment := _moment_of(candidate.timestamp)) is not None
    ]
    if not timed:
        return candidates[-1]
    return candidates[min(timed)[1]]


@pure
def resolve_author(
    content: str, event_timestamp: str, annotations: Sequence[AuthorAnnotation]
) -> tuple[ParticipantKey, str] | None:
    """The author of a turn with ``content``: by its text hash, else by its prefix (an annotated name, else a
    synthetic key), else None for the owner."""
    digest = text_sha256(content)
    by_hash = _nearest([annotation for annotation in annotations if annotation.text_sha256 == digest], event_timestamp)
    if by_hash is not None:
        return by_hash.author, by_hash.display_name
    name, _rest = split_author_prefix(content)
    if name is None:
        return None
    by_name = _nearest([annotation for annotation in annotations if annotation.display_name == name], event_timestamp)
    if by_name is not None:
        return by_name.author, by_name.display_name
    return name_key(name), name


def stamp_authors(events: Sequence[dict[str, Any]], annotations: Sequence[AuthorAnnotation]) -> None:
    """Stamp ``author`` and ``author_name`` on every ``user_message`` event that has an author, in place (the stored
    events are the same dicts, so a reload sees the same stamps); an event already stamped is left alone."""
    for event in events:
        if event.get("type") != "user_message" or AUTHOR_FIELD in event:
            continue
        content = event.get("content")
        if not isinstance(content, str):
            continue
        resolved = resolve_author(content, str(event.get("timestamp", "")), annotations)
        if resolved is None:
            continue
        event[AUTHOR_FIELD] = str(resolved[0])
        event[AUTHOR_NAME_FIELD] = resolved[1]


class _CachedAnnotations(MutableModel):
    """One chat's annotations as last read, and the file stamp they were read at."""

    model_config = {"extra": "forbid", "frozen": False}

    stamp: tuple[int, int] | None = Field(description="The file's (mtime_ns, size) at the read; None for no file")
    annotations: tuple[AuthorAnnotation, ...] = Field(description="The lines, in file order")


class AuthorStore(MutableModel):
    """Each chat's ``authors.jsonl``: appended by the send route, read (and cached by the file's stamp) for stamping."""

    model_config = {"extra": "forbid", "frozen": False}

    root: Path = Field(frozen=True, description="The chats directory (``data/.apps/chat/chats``)")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _cache_by_chat: dict[str, _CachedAnnotations] = PrivateAttr(default_factory=dict)

    def _path(self, chat_id: ChatId) -> Path:
        return self.root / str(chat_id) / AUTHORS_FILENAME

    def record(self, chat_id: ChatId, annotation: AuthorAnnotation) -> None:
        path = self._path(chat_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as authors_file:
                authors_file.write(annotation.model_dump_json() + "\n")

    def annotations(self, chat_id: ChatId) -> tuple[AuthorAnnotation, ...]:
        """The chat's annotations, re-read when the file changed since the last read."""
        path = self._path(chat_id)
        try:
            stat = path.stat()
            stamp: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            stamp = None
        with self._lock:
            cached = self._cache_by_chat.get(str(chat_id))
            if cached is not None and cached.stamp == stamp:
                return cached.annotations
            annotations = _read_annotations(path) if stamp is not None else ()
            self._cache_by_chat[str(chat_id)] = _CachedAnnotations(stamp=stamp, annotations=annotations)
            return annotations

    def stamp_events(self, chat_id: ChatId, events: Sequence[dict[str, Any]]) -> None:
        """Stamp the authors of a chat's events, in place: a chat with no annotations still stamps a hand-typed
        prefix with its synthetic key."""
        stamp_authors(events, self.annotations(chat_id))


def _read_annotations(path: Path) -> tuple[AuthorAnnotation, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        logger.warning("Could not read the authors of {}: {}", path.parent.name, e)
        return ()
    annotations: list[AuthorAnnotation] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            annotations.append(AuthorAnnotation.model_validate(json.loads(line)))
        except (ValueError, ValidationError) as e:
            logger.warning("Skipped an unreadable author line of {}: {}", path.parent.name, e)
    return tuple(annotations)
