"""The roster (the multiplayer spec, section 4.2): everyone who may be in the workspace, in display order.

The owner first (under ``user:<id>`` once they have heartbeated as an account, else the synthetic ``owner`` key),
then every account the people's grants name, then every invited email, then every visiting agent the gateway's
grants name, and the workspace's own agent last. A person's name and picture are their profile; an invited email
has neither; a visiting agent has the name it joined under; the workspace agent is named after the workspace and
pictured by the selected avatar design. Presence comes from the heartbeat files for people and from the gateway's
last-seen record for visiting agents, under one connected window. Handles are assigned in roster order, so a
collision suffixes the later entry; the workspace agent's ``agent`` is reserved before anyone else's.

``GET /api/roster`` answers the document, and ``roster_updated`` carries the same body whenever presence changes, a
grants file changes (the desktop and the gateway both write them, so the shell watches them), and on connect.
"""

import threading
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Final

from pydantic import Field
from pydantic import PrivateAttr
from share_grants.agent_grants import AgentGrant
from share_grants.agent_grants import read_agent_last_seen_times
from share_grants.grants import Grants
from watchdog.observers.api import BaseObserver
from workspace_layout.primitives import OWNER_PARTICIPANT_KEY
from workspace_layout.primitives import ParticipantKey
from workspace_layout.primitives import ParticipantKind
from workspace_layout.primitives import UserId
from workspace_layout.primitives import WORKSPACE_AGENT_PARTICIPANT_KEY
from workspace_layout.roster import RosterDocument
from workspace_layout.roster import RosterParticipant
from workspace_layout.roster import assign_handles
from workspace_layout.roster import participant_color
from workspace_layout.shell_url import ROSTER_ROUTE

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.avatar.selection import AvatarSelectionStore
from imbue.system_interface.file_watch import start_file_watch
from imbue.system_interface.file_watch import stop_file_watch
from imbue.system_interface.presence import PRESENCE_CONNECTED_WINDOW
from imbue.system_interface.presence import ParticipantPresence
from imbue.system_interface.presence import PresenceStore
from imbue.system_interface.profiles import ProfileResolver
from imbue.system_interface.profiles import UserProfile
from imbue.system_interface.profiles import picture_version_of
from imbue.system_interface.shell.share_grants import AgentGrantsReader
from imbue.system_interface.shell.share_grants import ShareGrantsReader
from imbue.system_interface.shell.users import UserStore
from imbue.system_interface.workspace_name import WorkspaceNameSource
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

# The workspace agent's handle (``@agent``), reserved before anyone else's is assigned.
WORKSPACE_AGENT_HANDLE: Final[str] = "agent"
# What the owner of a workspace that has not shared is called: nobody else is here to tell them apart from.
ANONYMOUS_OWNER_NAME: Final[str] = "Owner"
# The pixel size the roster asks the avatar's icon at: the biggest the cards draw it.
_AVATAR_ICON_SIZE: Final[int] = 64
# How often a watched file whose directory does not exist yet is tried again.
ROSTER_WATCH_RETRY_SECONDS: Final[float] = 10.0


class _Entry(FrozenModel):
    """One roster entry before its handle is assigned."""

    key: ParticipantKey = Field(description="The participant's key")
    kind: ParticipantKind = Field(description="What the participant is")
    display_name: str = Field(description="What to call them")
    handle_name: str = Field(description="What their handle is made from: the name, or an invited email's local part")
    profile_picture_url: str | None = Field(description="Their picture, when there is one")
    email: str | None = Field(description="Their email, when known")
    is_owner: bool = Field(description="Whether this is the owner")
    is_connected: bool = Field(description="Whether they are here now")
    last_seen: str | None = Field(description="When they were last here")
    last_input_at: str | None = Field(description="When their newest page last saw input")


@pure
def participant_picture_route(key: ParticipantKey, profile_picture_url: str) -> str:
    """Where the shell serves a person's picture from its own origin (the multiplayer spec, section 4.2): the
    roster's picture route, versioned by the connector's URL so a page never shows a cached older picture."""
    return f"{ROSTER_ROUTE}/{key}/picture?v={picture_version_of(profile_picture_url)}"


@pure
def _email_local_part(email: str | None) -> str | None:
    if email is None or "@" not in email:
        return None
    local_part = email.split("@", 1)[0]
    return local_part if local_part else None


@pure
def _person_name(profile: UserProfile | None, known_name: str | None, email: str | None, user_id: str) -> str:
    """A person's name: their profile's, else the one recorded at their last arrival, else their email's local
    part, else their id."""
    if profile is not None and profile.display_name:
        return profile.display_name
    if known_name:
        return known_name
    local_part = _email_local_part(email)
    return local_part if local_part is not None else user_id


@pure
def _format_moment(moment: datetime) -> str:
    return format_nanosecond_iso_timestamp(moment)


@pure
def _anonymous_owner_entry(presence: ParticipantPresence | None) -> _Entry:
    """The owner of a workspace that has not shared: no account, so a fixed name and no profile, present as their
    heartbeats under the ``owner`` key say."""
    return _Entry(
        key=OWNER_PARTICIPANT_KEY,
        kind=ParticipantKind.PERSON,
        display_name=ANONYMOUS_OWNER_NAME,
        handle_name=ANONYMOUS_OWNER_NAME,
        profile_picture_url=None,
        email=None,
        is_owner=True,
        is_connected=presence.is_connected if presence is not None else False,
        last_seen=presence.last_seen if presence is not None else None,
        last_input_at=presence.last_input_at if presence is not None else None,
    )


@pure
def _with_handles(entries: list[_Entry]) -> list[RosterParticipant]:
    """The entries as the wire carries them: handles assigned in order, the agent's reserved, each with its colour."""
    people = [entry for entry in entries if entry.kind is not ParticipantKind.WORKSPACE_AGENT]
    handles = assign_handles([entry.handle_name for entry in people], taken=[WORKSPACE_AGENT_HANDLE])
    handle_by_key = {entry.key: handle for entry, handle in zip(people, handles, strict=True)}
    return [
        RosterParticipant(
            key=entry.key,
            kind=entry.kind,
            display_name=entry.display_name,
            profile_picture_url=entry.profile_picture_url,
            email=entry.email,
            handle=handle_by_key.get(entry.key, WORKSPACE_AGENT_HANDLE),
            color=participant_color(entry.key),
            is_owner=entry.is_owner,
            is_connected=entry.is_connected,
            last_seen=entry.last_seen,
            last_input_at=entry.last_input_at,
        )
        for entry in entries
    ]


class RosterSource(MutableModel):
    """Computes the roster from the grants, the presence files, the profiles, and the gateway's last-seen record."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    presence: PresenceStore = Field(frozen=True, description="The heartbeat files of everyone who has been here")
    profiles: ProfileResolver = Field(frozen=True, description="Each account's name and picture")
    users: UserStore = Field(frozen=True, description="What the shell recorded of each visitor at their arrivals")
    share_grants: ShareGrantsReader = Field(frozen=True, description="The people's grants")
    agent_grants: AgentGrantsReader = Field(frozen=True, description="The visiting agents' grants")
    agent_last_seen_path: Path = Field(frozen=True, description="The gateway's record of each agent's last request")
    workspace_name: WorkspaceNameSource = Field(frozen=True, description="What the workspace agent is named after")
    avatar_selection: AvatarSelectionStore = Field(frozen=True, description="The design the agent is pictured by")
    connected_window: timedelta = Field(
        default=PRESENCE_CONNECTED_WINDOW, frozen=True, description="How long a sighting counts as connected"
    )

    def owner_key(self, now: datetime) -> ParticipantKey:
        """The owner's key as the presence records say it (the multiplayer spec, section 4.4): the account whose
        heartbeats say it owns the workspace, else the synthetic ``owner`` of a workspace that has not shared."""
        return self._owner_key(self.presence.presence_by_participant(now))

    def document(self, now: datetime) -> RosterDocument:
        presence_by_key = self.presence.presence_by_participant(now)
        grants = self.share_grants.read_grants()
        entries = [
            *self._people(grants, presence_by_key, now),
            *self._invited(grants),
            *self._visiting_agents(now),
            self._workspace_agent(),
        ]
        return RosterDocument(participants=tuple(_with_handles(entries)))

    def _people(
        self, grants: Grants | None, presence_by_key: dict[ParticipantKey, ParticipantPresence], now: datetime
    ) -> list[_Entry]:
        """The owner first, then every account the grants name, each named and pictured from its profile."""
        owner_key = self._owner_key(presence_by_key)
        granted_ids = sorted(grants.workspace.users) if grants is not None else []
        keys = [owner_key, *(ParticipantKey(f"user:{user_id}") for user_id in granted_ids)]
        # The owner may be granted too; they are listed once, first.
        ordered = list(dict.fromkeys(keys))
        user_ids = [UserId(str(key).removeprefix("user:")) for key in ordered if key != OWNER_PARTICIPANT_KEY]
        profile_by_user_id = self.profiles.resolve_many(user_ids, now)
        known_by_user_id = {str(record.user_id): record for record in self.users.list_users()}
        entries: list[_Entry] = []
        for key in ordered:
            presence = presence_by_key.get(key)
            if key == OWNER_PARTICIPANT_KEY:
                entries.append(_anonymous_owner_entry(presence))
                continue
            user_id = str(key).removeprefix("user:")
            profile = profile_by_user_id.get(user_id)
            known = known_by_user_id.get(user_id)
            email = (presence.email if presence is not None and presence.email else None) or (
                known.email if known is not None else None
            )
            display_name = _person_name(profile, known.display_name if known is not None else None, email, user_id)
            entries.append(
                _Entry(
                    key=key,
                    kind=ParticipantKind.PERSON,
                    display_name=display_name,
                    handle_name=display_name,
                    profile_picture_url=(
                        participant_picture_route(key, profile.profile_picture_url)
                        if profile is not None and profile.profile_picture_url is not None
                        else None
                    ),
                    email=email,
                    is_owner=key == owner_key,
                    is_connected=presence.is_connected if presence is not None else False,
                    last_seen=presence.last_seen if presence is not None else None,
                    last_input_at=presence.last_input_at if presence is not None else None,
                )
            )
        return entries

    def _owner_key(self, presence_by_key: dict[ParticipantKey, ParticipantPresence]) -> ParticipantKey:
        """The owner's key: the account whose heartbeats say it owns the workspace, else the synthetic one."""
        for key, presence in presence_by_key.items():
            if presence.owner and key != OWNER_PARTICIPANT_KEY:
                return key
        return OWNER_PARTICIPANT_KEY

    def _invited(self, grants: Grants | None) -> list[_Entry]:
        """Every email the grants still name: invited, not yet signed up, with no profile and no presence."""
        if grants is None:
            return []
        return [
            _Entry(
                key=ParticipantKey(f"email:{email}"),
                kind=ParticipantKind.INVITED,
                display_name=email,
                handle_name=_email_local_part(email) or email,
                profile_picture_url=None,
                email=email,
                is_owner=False,
                is_connected=False,
                last_seen=None,
                last_input_at=None,
            )
            for email in sorted(grants.workspace.emails)
        ]

    def _visiting_agents(self, now: datetime) -> list[_Entry]:
        """Every unexpired agent grant, connected while the gateway saw it within the window."""
        last_seen_by_id = read_agent_last_seen_times(self.agent_last_seen_path)
        return [
            self._visiting_agent(grant, last_seen_by_id.get(grant.agent_id), now)
            for grant in self.agent_grants.read_active_grants(now)
        ]

    def _visiting_agent(self, grant: AgentGrant, last_seen: datetime | None, now: datetime) -> _Entry:
        return _Entry(
            key=ParticipantKey(f"agent:{grant.agent_id}"),
            kind=ParticipantKind.VISITING_AGENT,
            display_name=grant.name,
            handle_name=grant.name,
            profile_picture_url=None,
            email=None,
            is_owner=False,
            is_connected=last_seen is not None and now - last_seen < self.connected_window,
            last_seen=_format_moment(last_seen) if last_seen is not None else None,
            last_input_at=None,
        )

    def _workspace_agent(self) -> _Entry:
        """The workspace's own agent: named after the workspace, pictured by the selected design's icon."""
        design = self.avatar_selection.read()
        return _Entry(
            key=WORKSPACE_AGENT_PARTICIPANT_KEY,
            kind=ParticipantKind.WORKSPACE_AGENT,
            display_name=self.workspace_name.resolve(),
            handle_name=WORKSPACE_AGENT_HANDLE,
            profile_picture_url=f"/api/avatars/{design}/icon.png?size={_AVATAR_ICON_SIZE}",
            email=None,
            is_owner=False,
            is_connected=True,
            last_seen=None,
            last_input_at=None,
        )


def build_roster_announcer(
    roster: RosterSource, broadcaster: WebSocketBroadcaster, now: Callable[[], datetime]
) -> Callable[[], None]:
    """How the roster reaches every window: computed afresh and pushed as ``roster_updated``."""
    return lambda: broadcaster.broadcast_roster_updated(roster.document(now()).model_dump(mode="json"))


class RosterWatch(MutableModel):
    """Announces the roster whenever one of the files it is computed from changes: the two grants documents and the
    gateway's last-seen record. The gateway creates the agent-access directories when the workspace is first shared,
    which is after the shell started on a fresh workspace, so a file whose directory does not exist yet is tried
    again every ``retry_interval_seconds`` until it is watched, and a watch that starts late announces the roster
    once, since the file may have been written before it began."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    watched_paths: tuple[Path, ...] = Field(frozen=True, description="The files whose change re-announces the roster")
    announce: Callable[[], None] = Field(frozen=True, description="How the roster reaches the windows")
    retry_interval_seconds: float = Field(
        default=ROSTER_WATCH_RETRY_SECONDS, frozen=True, description="How often a not-yet-watchable file is tried"
    )

    _observers: list[BaseObserver] = PrivateAttr(default_factory=list)
    _pending: list[Path] = PrivateAttr(default_factory=list)
    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)

    @property
    def pending_paths(self) -> tuple[Path, ...]:
        """The files not watched yet, their directories still missing."""
        return tuple(self._pending)

    def start(self) -> None:
        self._pending = list(self.watched_paths)
        self._watch_pending()
        if self._pending:
            thread = threading.Thread(target=self._run, daemon=True, name="roster-watch")
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        for observer in self._observers:
            stop_file_watch(observer)
        self._observers = []

    def _watch_pending(self) -> bool:
        """Start the watch of every pending file whose directory now exists; whether any started."""
        still_pending: list[Path] = []
        is_any_started = False
        for path in self._pending:
            if not path.parent.is_dir():
                still_pending.append(path)
                continue
            observer = start_file_watch(path, self.announce)
            if observer is not None:
                self._observers.append(observer)
                is_any_started = True
        self._pending = still_pending
        return is_any_started

    def _run(self) -> None:
        while not self._stop.wait(timeout=self.retry_interval_seconds):
            if self._watch_pending():
                self.announce()
            if not self._pending:
                return
