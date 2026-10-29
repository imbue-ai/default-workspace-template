"""Workspace presence: who is connected to the shell right now, and when everyone else was last here.

Every request that reaches the shell carries the requester's identity in the
``X-Imbue-Identity`` header, stamped by whichever proxy admitted it (the local
forward or the share gateway; the header contract is documented in the share
gateway's README). The shell page heartbeats this endpoint while it is visible.
Each user has one file under ``data/.state/presence/users/``, written on first
sight with their identity snapshot and rewritten only when that snapshot changes;
a heartbeat merely touches the file's mtime. A user is connected while the mtime
is within ``PRESENCE_CONNECTED_WINDOW``, and the mtime is their "last seen". Files
are never removed: a user who is gone is still the record of who was last here.

The shell pushes the connected set over its WebSocket whenever it changes: a
heartbeat that brings someone in announces it at once, and a periodic sweep
notices a silent departure (a closed tab whose heartbeats stopped).

A request whose identity has no user id is the owner of a workspace that has not
shared (or one that came through no proxy at all, which is read the same way):
it is recorded under the synthetic ``owner`` key (the multiplayer spec, S12),
so the owner's heartbeats count whether or not they have revealed who they
are. That record is left out of the ``presence_updated`` push, which names
accounts; the roster carries it instead. A visiting agent never heartbeats:
its presence is the last-seen record the share gateway keeps, which the roster
reads beside these files.

Each heartbeat may also carry how long ago the page last saw keyboard or pointer
input, which the store stamps against its own clock and keeps in the file beside the
identity snapshot (``last_input_at``), never moving it backwards; it is what tells an
active person from an idle one. A heartbeat that moves it is reported as such, so the
shell can announce the roster that carries it (the multiplayer spec, section 4.3).
"""

import os
import threading
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError
from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.identity import RequestIdentity
from workspace_layout.primitives import OWNER_PARTICIPANT_KEY
from workspace_layout.primitives import OWNER_PARTICIPANT_KEY_VALUE
from workspace_layout.primitives import ParticipantKey
from workspace_layout.primitives import UserId

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.profiles import ProfileResolver
from imbue.system_interface.profiles import UserProfile
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.errors import ShellStateError
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

# Where presence lives, relative to the workspace root the supervised process runs from
# (machine state, like the shell's own files); ``main.py`` takes ``--presence-dir`` so a
# test can point elsewhere.
DEFAULT_PRESENCE_DIRECTORY: Final[Path] = Path("data/.state/presence")
USERS_DIRECTORY_NAME: Final[str] = "users"

# The shell heartbeats every 30 seconds while visible; a user whose file has not been touched
# for two heartbeats plus some slack is gone (a closed tab, a suspended laptop).
PRESENCE_CONNECTED_WINDOW: Final[timedelta] = timedelta(seconds=70)
# How often the sweep looks for a departure nobody's heartbeat has revealed.
PRESENCE_SWEEP_INTERVAL_SECONDS: Final[float] = 10.0

_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=timezone.utc)


class PresentUser(FrozenModel):
    """One user who has been here: their identity snapshot and when they were first and last seen."""

    user_id: str = Field(description="The account's user id (the file name under users/)")
    email: str = Field(description="The account's verified email as of its last heartbeat")
    owner: bool = Field(description="Whether this user owns the workspace")
    first_seen: str = Field(description="ISO timestamp of the heartbeat that created this record")
    last_seen: str = Field(description="ISO timestamp of the latest heartbeat (the file's mtime)")
    last_input_at: str | None = Field(
        default=None, description="ISO timestamp of the newest keyboard or pointer input a page of theirs reported"
    )

    @property
    def is_anonymous_owner(self) -> bool:
        """Whether this is the owner recorded under the synthetic ``owner`` key rather than an account."""
        return self.user_id == OWNER_PARTICIPANT_KEY_VALUE

    @property
    def participant(self) -> ParticipantKey:
        return OWNER_PARTICIPANT_KEY if self.is_anonymous_owner else ParticipantKey(f"user:{self.user_id}")


class _StoredPresence(FrozenModel):
    """The body of ``users/<user_id>.json``: the identity snapshot; the heartbeats are the file's mtime."""

    model_config = ConfigDict(extra="ignore")

    user_id: str = Field(description="The account's user id")
    email: str = Field(description="The verified email as last stamped by a proxy")
    owner: bool = Field(description="Whether this user owns the workspace")
    first_seen: str = Field(description="ISO timestamp of the heartbeat that created the file")
    last_input_at: str | None = Field(
        default=None, description="ISO timestamp of the newest input a page reported, when any did"
    )


class PresenceOutcome(FrozenModel):
    """What a heartbeat or a sweep found: the connected set now, whether it differs from the last one announced, and
    whether the heartbeat moved the requester's newest input time."""

    users: tuple[PresentUser, ...] = Field(description="Every connected user now, by user id")
    is_membership_changed: bool = Field(
        description="Whether the connected set differs from the one last reported as changed (what the shell broadcasts on)"
    )
    is_input_changed: bool = Field(
        default=False,
        description="Whether the heartbeat moved the requester's stored newest input time (a sweep never does)",
    )


@pure
def _format_timestamp(moment: datetime) -> str:
    return format_nanosecond_iso_timestamp(moment)


@pure
def _nanoseconds_since_epoch(moment: datetime) -> int:
    return ((moment - _EPOCH) // timedelta(microseconds=1)) * 1000


@pure
def _moment_of_nanoseconds(nanoseconds: int) -> datetime:
    return _EPOCH + timedelta(microseconds=nanoseconds // 1000)


@pure
def present_user_wire_json(user: PresentUser, profile: UserProfile | None) -> dict[str, Any]:
    """One user as the WebSocket and the presence route serialize it: the record with the account's profile."""
    return {
        "user_id": user.user_id,
        "email": user.email,
        "display_name": profile.display_name if profile is not None else None,
        "profile_picture_url": profile.profile_picture_url if profile is not None else None,
        "owner": user.owner,
        "first_seen": user.first_seen,
        "last_seen": user.last_seen,
        "last_input_at": user.last_input_at,
    }


def present_users_wire_json(
    users: Sequence[PresentUser], profiles: ProfileResolver, now: datetime
) -> list[dict[str, Any]]:
    """The connected accounts as the wire carries them, each with the profile the resolver has for it; the owner
    recorded under the ``owner`` key names no account and is left to the roster."""
    accounts = [user for user in users if not user.is_anonymous_owner]
    profile_by_user_id = profiles.resolve_many([UserId(user.user_id) for user in accounts], now)
    return [present_user_wire_json(user, profile_by_user_id.get(user.user_id)) for user in accounts]


class PresenceStore(MutableModel):
    """The per-user presence files, written under one process-wide lock."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    directory: Path = Field(frozen=True, description="Where the users/ files live")
    connected_window: timedelta = Field(
        default=PRESENCE_CONNECTED_WINDOW, frozen=True, description="How long a user outlives their last heartbeat"
    )
    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)
    # The connected set as last reported changed, so a heartbeat and the sweep agree on what the windows know.
    _announced_user_ids: frozenset[str] = PrivateAttr(default=frozenset())

    @property
    def users_directory(self) -> Path:
        return self.directory / USERS_DIRECTORY_NAME

    def heartbeat(
        self, identity: RequestIdentity, now: datetime, last_input_at: datetime | None = None
    ) -> PresenceOutcome:
        """Record one heartbeat: write the user's file on first sight (or when their email, owner flag, or newest
        input changed), and touch its mtime to ``now``. The owner of a workspace that has not shared is recorded
        under the ``owner`` key. The input time is taken no later than ``now`` (a page's clock is not the shell's)
        and never moves backwards; the outcome says whether it moved."""
        user_id = _require_user_id(identity)
        with self._lock:
            path = self._user_path(str(user_id))
            existing = self._read_stored(path)
            snapshot = _StoredPresence(
                user_id=str(user_id),
                email=identity.email or "",
                owner=identity.owner,
                first_seen=existing.first_seen if existing is not None else _format_timestamp(now),
                last_input_at=_newest_input(existing, _no_later_than(last_input_at, now)),
            )
            if snapshot != existing:
                write_json_atomic(path, snapshot.model_dump())
            self._touch(path, now)
            is_input_changed = snapshot.last_input_at != (existing.last_input_at if existing is not None else None)
            return self._outcome(now, is_input_changed)

    def sweep(self, now: datetime) -> PresenceOutcome:
        """The connected set now, flagged changed when someone has silently gone (or come) since the last report."""
        with self._lock:
            return self._outcome(now)

    def connected_users(self, now: datetime) -> list[PresentUser]:
        """Every user whose last heartbeat is within the connected window, by user id."""
        return [user for user in self.all_users() if now - _parse_timestamp(user.last_seen) < self.connected_window]

    def presence_by_participant(self, now: datetime) -> dict[ParticipantKey, "ParticipantPresence"]:
        """What the files say of each participant that has ever heartbeated: whether they are connected now, and
        when they were last here and last typed or clicked."""
        return {
            user.participant: ParticipantPresence(
                is_connected=now - _parse_timestamp(user.last_seen) < self.connected_window,
                last_seen=user.last_seen,
                last_input_at=user.last_input_at,
                owner=user.owner,
                email=user.email,
            )
            for user in self.all_users()
        }

    def all_users(self) -> list[PresentUser]:
        """Everyone with a presence file, connected or not (the "last here" records), by user id."""
        with self._lock:
            if not self.users_directory.is_dir():
                return []
            users: list[PresentUser] = []
            for path in sorted(self.users_directory.glob("*.json")):
                try:
                    UserId(path.stem)
                except InvalidLayoutValueError:
                    logger.warning("Skipped a presence file whose name is not a user id: {}", path.name)
                    continue
                user = self._read_user(path)
                if user is not None:
                    users.append(user)
            return users

    def _outcome(self, now: datetime, is_input_changed: bool = False) -> PresenceOutcome:
        connected = tuple(self.connected_users(now))
        connected_user_ids = frozenset(user.user_id for user in connected)
        is_membership_changed = connected_user_ids != self._announced_user_ids
        if is_membership_changed:
            self._announced_user_ids = connected_user_ids
        return PresenceOutcome(
            users=connected, is_membership_changed=is_membership_changed, is_input_changed=is_input_changed
        )

    def _user_path(self, user_id: str) -> Path:
        return self.users_directory / f"{user_id}.json"

    def _read_stored(self, path: Path) -> _StoredPresence | None:
        document = read_json_object(path)
        if document is None:
            return None
        try:
            return _StoredPresence.model_validate(document)
        except ValidationError as e:
            logger.warning("Skipped an unreadable presence record {}: {}", path.name, e.errors()[0]["msg"])
            return None

    def _read_user(self, path: Path) -> PresentUser | None:
        stored = self._read_stored(path)
        if stored is None:
            return None
        try:
            modified_at_nanoseconds = path.stat().st_mtime_ns
        except OSError as e:
            logger.warning("Skipped a presence record whose file could not be inspected {}: {}", path.name, e)
            return None
        return PresentUser(
            user_id=stored.user_id,
            email=stored.email,
            owner=stored.owner,
            first_seen=stored.first_seen,
            last_seen=_format_timestamp(_moment_of_nanoseconds(modified_at_nanoseconds)),
            last_input_at=stored.last_input_at,
        )

    def _touch(self, path: Path, now: datetime) -> None:
        nanoseconds = _nanoseconds_since_epoch(now)
        try:
            os.utime(path, ns=(nanoseconds, nanoseconds))
        except OSError as e:
            raise ShellStateError(f"cannot touch the presence file {path}: {e}") from e


@pure
def _parse_timestamp(value: str) -> datetime:
    # The nanosecond formatter writes nine fraction digits and a trailing Z; fromisoformat
    # reads at most six, so the fraction is cut to microseconds first.
    return datetime.fromisoformat(value[:26] + "+00:00")


def _require_user_id(identity: RequestIdentity) -> UserId:
    """The file a requester's presence is kept under: their user id, or ``owner`` for the owner of a workspace
    that has not shared (S12); a visiting agent never heartbeats, so it has no file here."""
    if identity.agent_id is not None:
        raise InvalidShellValueError("presence needs a person (the identity names a visiting agent)")
    if identity.user_id is not None:
        return UserId(identity.user_id)
    return UserId(OWNER_PARTICIPANT_KEY_VALUE)


@pure
def reported_input_moment(
    now: datetime, input_age_seconds: float | None, last_input_at: datetime | None
) -> datetime | None:
    """When a heartbeat says the page last saw input, on the shell's clock: ``now`` less the age the page reported,
    which no clock skew can move; a page from before the age was sent reports a moment by its own clock instead,
    which is taken as it is (the store clamps it to ``now``); None when the page has seen none."""
    if input_age_seconds is not None:
        return now - timedelta(seconds=input_age_seconds)
    return last_input_at


@pure
def _no_later_than(moment: datetime | None, now: datetime) -> datetime | None:
    """A reported moment, clamped to the shell's now: a clock ahead of the shell's cannot pin a person active."""
    if moment is None:
        return None
    return min(moment, now)


@pure
def _newest_input(existing: _StoredPresence | None, reported: datetime | None) -> str | None:
    """The newest input time of the record and the report: a late heartbeat never moves it backwards."""
    reported_text = _format_timestamp(reported) if reported is not None else None
    if existing is None or existing.last_input_at is None:
        return reported_text
    if reported_text is None:
        return existing.last_input_at
    return max(existing.last_input_at, reported_text)


class ParticipantPresence(FrozenModel):
    """What presence knows of one participant: whether they are here now, and when they were last here and active."""

    is_connected: bool = Field(description="Whether their last heartbeat is within the connected window")
    last_seen: str = Field(description="ISO timestamp of their last heartbeat")
    last_input_at: str | None = Field(description="ISO timestamp of their newest reported input, when any")
    owner: bool = Field(description="Whether the record says they own the workspace")
    email: str = Field(description="The email their last heartbeat carried; empty for the anonymous owner")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PresenceSweep(MutableModel):
    """The periodic check that notices a silent departure and announces the connected set when it changed."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    store: PresenceStore = Field(frozen=True, description="The presence files the sweep reads")
    interval_seconds: float = Field(
        default=PRESENCE_SWEEP_INTERVAL_SECONDS, frozen=True, description="How often the sweep runs"
    )
    announce: Callable[[Sequence[PresentUser]], None] = Field(
        frozen=True, description="How a changed connected set reaches the windows"
    )
    announce_roster: Callable[[], None] = Field(
        frozen=True, description="How the roster, which a changed connected set changes too, reaches the windows"
    )

    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)

    def start(self) -> None:
        thread = threading.Thread(target=self._run, daemon=True, name="presence-sweep")
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def is_running(self) -> bool:
        """Whether the sweep thread is alive: started, and not yet exited after a stop."""
        return self._thread is not None and self._thread.is_alive()

    def sweep_once(self, now: datetime) -> bool:
        """One pass: announce the connected set when it differs from the last announced one; answers whether it did."""
        outcome = self.store.sweep(now)
        if outcome.is_membership_changed:
            self.announce(outcome.users)
            self.announce_roster()
        return outcome.is_membership_changed

    def _run(self) -> None:
        while not self._stop.wait(timeout=self.interval_seconds):
            # A directory that cannot be read this time is logged and read again next time.
            try:
                self.sweep_once(utc_now())
            except (OSError, ValueError) as e:
                logger.opt(exception=e).error("The presence sweep failed; the next run will retry")


def build_presence_sweep(
    store: PresenceStore,
    profiles: ProfileResolver,
    broadcaster: WebSocketBroadcaster,
    announce_roster: Callable[[], None],
) -> PresenceSweep:
    """A sweep that announces the connected set to every window, each user carrying their profile, and the roster
    after it."""
    return PresenceSweep(
        store=store,
        announce=lambda users: broadcaster.broadcast_presence_updated(
            present_users_wire_json(users, profiles, utc_now())
        ),
        announce_roster=announce_roster,
    )
