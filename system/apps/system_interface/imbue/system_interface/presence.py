"""Workspace presence: who is connected to the shell right now, with their identity details.

Every request that reaches the shell carries the requester's identity in the
``X-Imbue-Identity`` header, stamped by whichever proxy admitted it (the local
forward or the share gateway; the header contract is documented in the share
gateway's README). The shell page heartbeats this endpoint while it is visible,
and each heartbeat upserts one file per user under ``data/.state/presence/users/``
-- the latest identity snapshot for that user plus the tab sessions it has open.
A session that stops heartbeating expires after ``PRESENCE_SESSION_TTL``; a user
whose last session expired or left is removed, and joins and leaves are appended
to ``data/.state/presence/events.jsonl`` in the repo's event envelope. Apps read
the directory or tail the events; the shell pushes the connected set over its
WebSocket.

A request whose identity has no user id (the owner of an unshared workspace,
or one that came through no proxy at all) is never recorded: there is nobody
to name.
"""

import json
import re
import threading
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from typing import Self

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.errors import ShellStateError
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

IDENTITY_HEADER: Final[str] = "X-Imbue-Identity"

# Where presence lives, relative to the workspace root the supervised process runs from
# (machine state, like the shell's own files); ``main.py`` takes ``--presence-dir`` so a
# test can point elsewhere.
DEFAULT_PRESENCE_DIRECTORY: Final[Path] = Path("data/.state/presence")
USERS_DIRECTORY_NAME: Final[str] = "users"
EVENTS_FILENAME: Final[str] = "events.jsonl"

# The shell heartbeats every 30 seconds while visible; a session that misses three in a
# row is gone (a closed tab whose leave beacon never arrived, a suspended laptop).
PRESENCE_SESSION_TTL: Final[timedelta] = timedelta(seconds=90)

PRESENCE_EVENT_SOURCE: Final[EventSource] = EventSource("presence")
USER_JOINED_EVENT_TYPE: Final[EventType] = EventType("user_joined")
USER_LEFT_EVENT_TYPE: Final[EventType] = EventType("user_left")

# A per-tab session id the shell page mints (a uuid); held to a filename-safe alphabet
# because it is written into the user's presence file as a key.
_SESSION_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
# A user id names the user's presence file, so it is held to the same alphabet.
_USER_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class PresenceSessionId(NonEmptyStr):
    """One shell tab's presence session (minted per page load by the shell)."""

    def __new__(cls, value: str) -> Self:
        if not _SESSION_ID_PATTERN.fullmatch(value):
            raise InvalidShellValueError(f"invalid presence session id {value!r}")
        return super().__new__(cls, value)


class PresenceUserId(NonEmptyStr):
    """A signed-in account's user id as the identity header carries it."""

    def __new__(cls, value: str) -> Self:
        if not _USER_ID_PATTERN.fullmatch(value):
            raise InvalidShellValueError(f"invalid presence user id {value!r}")
        return super().__new__(cls, value)


class RequestIdentity(FrozenModel):
    """The requester a proxy vouched for: the owner flag always, the account record when the workspace is shared."""

    # The header is cross-version wire data from the proxies; a field this build does not
    # know must never make it unreadable.
    model_config = ConfigDict(extra="ignore")

    owner: bool = Field(description="Whether the requester is the workspace's owner")
    user_id: str | None = Field(default=None, description="The account's user id (absent for an unshared workspace)")
    email: str | None = Field(default=None, description="The account's verified email, present exactly when user_id is")
    display_name: str | None = Field(default=None, description="The account's display name, when it has one")
    avatar_url: str | None = Field(default=None, description="The account's avatar URL, when it has one")


ANONYMOUS_OWNER: Final[RequestIdentity] = RequestIdentity(owner=True)


@pure
def parse_identity_header(header_value: str | None) -> RequestIdentity:
    """The identity a request carries; a missing or unreadable header reads as the anonymous owner.

    A missing header means the request came through no current proxy (an older
    forward, an in-container caller): it is treated as the owner with no account,
    never as a visitor. An unreadable one is logged and treated the same way.
    """
    if header_value is None or not header_value.strip():
        return ANONYMOUS_OWNER
    try:
        return RequestIdentity.model_validate_json(header_value)
    except ValidationError as e:
        logger.warning("Ignored an unreadable {} header: {}", IDENTITY_HEADER, e.errors()[0]["msg"])
        return ANONYMOUS_OWNER


class PresentUser(FrozenModel):
    """One connected user: the latest identity snapshot plus the tab sessions it has open."""

    user_id: str = Field(description="The account's user id (the file name under users/)")
    email: str = Field(description="The account's verified email as of its last heartbeat")
    display_name: str | None = Field(default=None, description="The display name as of its last heartbeat")
    avatar_url: str | None = Field(default=None, description="The avatar URL as of its last heartbeat")
    owner: bool = Field(description="Whether this user owns the workspace")
    sessions: dict[str, str] = Field(description="Each open tab's session id to the ISO timestamp of its last heartbeat")
    first_seen: str = Field(description="ISO timestamp of the heartbeat that created this record")
    last_seen: str = Field(description="ISO timestamp of the latest heartbeat from any session")


class PresenceOutcome(FrozenModel):
    """What a heartbeat or leave did: the connected set afterwards, and whether the recorded set changed."""

    users: tuple[PresentUser, ...] = Field(description="Every connected user after the change, by user id")
    is_membership_changed: bool = Field(
        description="Whether a user's record appeared or was removed, expiries included (what the shell broadcasts on)"
    )


class PresenceUserJoinedEvent(EventEnvelope):
    """A user's first open session appeared."""

    user_id: str = Field(description="The user who joined")
    email: str = Field(description="Their verified email at the time")
    display_name: str | None = Field(default=None, description="Their display name at the time")
    avatar_url: str | None = Field(default=None, description="Their avatar URL at the time")
    owner: bool = Field(description="Whether they own the workspace")


class PresenceUserLeftEvent(EventEnvelope):
    """A user's last open session left or expired."""

    user_id: str = Field(description="The user who left")
    email: str = Field(description="Their verified email as last seen")
    display_name: str | None = Field(default=None, description="Their display name as last seen")
    avatar_url: str | None = Field(default=None, description="Their avatar URL as last seen")
    owner: bool = Field(description="Whether they own the workspace")


def _format_timestamp(now: datetime) -> str:
    return format_nanosecond_iso_timestamp(now)


@pure
def parse_presence_timestamp(value: str) -> datetime | None:
    """The datetime a presence timestamp names, or None for one this build cannot read."""
    # The nanosecond formatter writes nine fraction digits and a trailing Z; fromisoformat
    # reads at most six, so the fraction is cut to microseconds first.
    if not value.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(value[:26] + "+00:00")
    except ValueError:
        return None


@pure
def present_user_wire_json(user: PresentUser) -> dict[str, Any]:
    """One user as the WebSocket and the presence route serialize it: the record with a session count."""
    return {
        "user_id": user.user_id,
        "email": user.email,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "owner": user.owner,
        "session_count": len(user.sessions),
        "first_seen": user.first_seen,
        "last_seen": user.last_seen,
    }


class PresenceStore(MutableModel):
    """The per-user presence files and the join/leave event log, written under one process-wide lock."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    directory: Path = Field(frozen=True, description="Where the users/ files and events.jsonl live")
    session_ttl: timedelta = Field(
        default=PRESENCE_SESSION_TTL, frozen=True, description="How long a session outlives its last heartbeat"
    )
    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)

    @property
    def users_directory(self) -> Path:
        return self.directory / USERS_DIRECTORY_NAME

    @property
    def events_path(self) -> Path:
        return self.directory / EVENTS_FILENAME

    def heartbeat(self, identity: RequestIdentity, session_id: PresenceSessionId, now: datetime) -> PresenceOutcome:
        """Record one tab's heartbeat: upsert the user's record with the request's identity snapshot, then prune."""
        user_id = _require_user_id(identity)
        with self._lock:
            before = self._recorded_user_ids()
            existing = self._read_user(user_id)
            sessions = dict(existing.sessions) if existing is not None else {}
            sessions[str(session_id)] = _format_timestamp(now)
            updated = PresentUser(
                user_id=str(user_id),
                email=identity.email or "",
                display_name=identity.display_name,
                avatar_url=identity.avatar_url,
                owner=identity.owner,
                sessions=sessions,
                first_seen=existing.first_seen if existing is not None else _format_timestamp(now),
                last_seen=_format_timestamp(now),
            )
            self._write_user(updated)
            if existing is None:
                self._append_joined(updated, now)
            self._prune_expired(now)
            return self._outcome(before, now)

    def leave(self, identity: RequestIdentity, session_id: PresenceSessionId, now: datetime) -> PresenceOutcome:
        """Drop one tab's session; the user leaves when it was their last one. Then prune."""
        user_id = _require_user_id(identity)
        with self._lock:
            before = self._recorded_user_ids()
            existing = self._read_user(user_id)
            if existing is not None and str(session_id) in existing.sessions:
                remaining = {key: value for key, value in existing.sessions.items() if key != str(session_id)}
                self._replace_sessions(existing, remaining, now)
            self._prune_expired(now)
            return self._outcome(before, now)

    def connected_users(self, now: datetime) -> list[PresentUser]:
        """Every user with at least one unexpired session, by user id (a read; nothing is pruned on disk)."""
        with self._lock:
            users = []
            for user in self._read_all_users():
                live_sessions = self._live_sessions(user, now)
                if live_sessions:
                    users.append(user.model_copy_update(to_update(user.field_ref().sessions, live_sessions)))
            return sorted(users, key=lambda user: user.user_id)

    def _outcome(self, before: set[str], now: datetime) -> PresenceOutcome:
        # Membership is judged on the records, not the TTL-filtered view: an expiry
        # removes a record the windows were last told about, so it must broadcast too.
        return PresenceOutcome(
            users=tuple(self.connected_users(now)), is_membership_changed=before != self._recorded_user_ids()
        )

    def _recorded_user_ids(self) -> set[str]:
        return {user.user_id for user in self._read_all_users()}

    def _live_sessions(self, user: PresentUser, now: datetime) -> dict[str, str]:
        live: dict[str, str] = {}
        for session_id, last_seen in user.sessions.items():
            seen_at = parse_presence_timestamp(last_seen)
            if seen_at is not None and now - seen_at < self.session_ttl:
                live[session_id] = last_seen
        return live

    def _prune_expired(self, now: datetime) -> None:
        for user in self._read_all_users():
            live_sessions = self._live_sessions(user, now)
            if live_sessions != user.sessions:
                self._replace_sessions(user, live_sessions, now)

    def _replace_sessions(self, user: PresentUser, sessions: dict[str, str], now: datetime) -> None:
        """Rewrite a user's sessions; an empty set removes the record and logs the leave."""
        if sessions:
            self._write_user(user.model_copy_update(to_update(user.field_ref().sessions, sessions)))
            return
        self._user_path(user.user_id).unlink(missing_ok=True)
        self._append_left(user, now)

    def _user_path(self, user_id: str) -> Path:
        return self.users_directory / f"{user_id}.json"

    def _read_user(self, user_id: PresenceUserId) -> PresentUser | None:
        document = read_json_object(self._user_path(str(user_id)))
        if document is None:
            return None
        try:
            return PresentUser.model_validate(document)
        except ValidationError as e:
            logger.warning("Skipped an unreadable presence record for {}: {}", user_id, e.errors()[0]["msg"])
            return None

    def _read_all_users(self) -> list[PresentUser]:
        if not self.users_directory.is_dir():
            return []
        users: list[PresentUser] = []
        for path in sorted(self.users_directory.glob("*.json")):
            try:
                user = self._read_user(PresenceUserId(path.stem))
            except InvalidShellValueError:
                logger.warning("Skipped a presence file whose name is not a user id: {}", path.name)
                continue
            if user is not None:
                users.append(user)
        return users

    def _write_user(self, user: PresentUser) -> None:
        write_json_atomic(self._user_path(user.user_id), user.model_dump())

    def _append_joined(self, user: PresentUser, now: datetime) -> None:
        self._append_event(
            PresenceUserJoinedEvent(
                timestamp=IsoTimestamp(_format_timestamp(now)),
                type=USER_JOINED_EVENT_TYPE,
                event_id=EventId(generate_log_event_id()),
                source=PRESENCE_EVENT_SOURCE,
                user_id=user.user_id,
                email=user.email,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
                owner=user.owner,
            )
        )

    def _append_left(self, user: PresentUser, now: datetime) -> None:
        self._append_event(
            PresenceUserLeftEvent(
                timestamp=IsoTimestamp(_format_timestamp(now)),
                type=USER_LEFT_EVENT_TYPE,
                event_id=EventId(generate_log_event_id()),
                source=PRESENCE_EVENT_SOURCE,
                user_id=user.user_id,
                email=user.email,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
                owner=user.owner,
            )
        )

    def _append_event(self, event: EventEnvelope) -> None:
        try:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as event_file:
                event_file.write(event.model_dump_json() + "\n")
        except OSError as e:
            raise ShellStateError(f"cannot append to the presence event log {self.events_path}: {e}") from e

    def read_events(self) -> list[dict[str, Any]]:
        """Every parseable event line, in file (chronological) order."""
        if not self.events_path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as e:
                logger.opt(exception=e).warning("Skipped an unparsable presence event line")
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        return events


def _require_user_id(identity: RequestIdentity) -> PresenceUserId:
    if identity.user_id is None:
        raise InvalidShellValueError("presence needs a signed-in requester (the identity carries no user id)")
    return PresenceUserId(identity.user_id)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
