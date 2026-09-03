import threading
from collections.abc import Mapping, Sequence
from typing import Final

from app_instances.data_types import InstanceLifetime, InstanceRecord, InstanceStatus
from app_instances.errors import (
    InstanceConflictError,
    InvalidInstanceValueError,
    InvalidParamsError,
    LocationNotTrackedError,
    UnknownActionError,
    UnknownInstanceError,
)
from app_instances.interfaces import InstanceSourceInterface
from app_instances.json_store import NEW_ACTION_ID, allocate_key
from app_instances.primitives import (
    InstanceKey,
    InstanceKeyPrefix,
    InstanceTitle,
    LocationPath,
    canonical_name_from_title,
    is_name_conflict,
)
from app_manifest.primitives import ActionId
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field, PrivateAttr

from terminal_app.data_types import TerminalSessionRecord, TmuxSession
from terminal_app.errors import InvalidTerminalValueError
from terminal_app.interfaces import TerminalSessionStoreInterface, TmuxInterface
from terminal_app.primitives import (
    TmuxSessionName,
    Workdir,
    derive_terminal_title,
    instance_url_for_session,
)

# The names the ``new`` action mints: the lowest free ``terminal-<N>``, as the shell's allocator
# minted them until now.
TERMINAL_KEY_PREFIX: Final[InstanceKeyPrefix] = InstanceKeyPrefix("terminal")

# The one parameter ``new`` accepts (contracts.md section 4.3).
WORKDIR_PARAM: Final[str] = "workdir"


@pure
def is_agent_session(name: str, agent_session_prefix: str) -> bool:
    """Whether a tmux session belongs to an mngr agent (its name carries the mngr prefix) rather than being a terminal.

    An empty prefix means no agent prefix is configured, so nothing is an agent session.
    """
    return bool(agent_session_prefix) and name.startswith(agent_session_prefix)


@pure
def _live_instance_record(
    session: TmuxSession, record: TerminalSessionRecord | None
) -> InstanceRecord:
    name = TmuxSessionName(session.name)
    return InstanceRecord(
        key=InstanceKey(name),
        url=instance_url_for_session(name, record.workdir if record else None),
        title=record.title if record and record.title else derive_terminal_title(name),
        status=InstanceStatus.IDLE,
        lifetime=InstanceLifetime.EXPLICIT,
        last_active=session.last_activity,
        renameable=True,
    )


@pure
def _stopped_instance_record(record: TerminalSessionRecord) -> InstanceRecord:
    return InstanceRecord(
        key=InstanceKey(record.name),
        url=instance_url_for_session(record.name, record.workdir),
        title=record.title if record.title else derive_terminal_title(record.name),
        status=InstanceStatus.STOPPED,
        lifetime=InstanceLifetime.EXPLICIT,
        last_active=None,
        renameable=True,
    )


@pure
def build_instance_records(
    live_sessions: Sequence[TmuxSession], records: Sequence[TerminalSessionRecord]
) -> list[InstanceRecord]:
    """Every live user session as ``idle`` (in tmux order), then every remembered terminal tmux no longer has as ``stopped``."""
    record_by_name = {record.name: record for record in records}
    live_names = {session.name for session in live_sessions}
    instances = [
        _live_instance_record(session, record_by_name.get(session.name))
        for session in live_sessions
    ]
    instances.extend(
        _stopped_instance_record(record)
        for record in records
        if record.name not in live_names
    )
    return instances


@pure
def _parse_workdir(params: Mapping[str, str]) -> Workdir | None:
    unknown_params = sorted(set(params) - {WORKDIR_PARAM})
    if unknown_params:
        raise InvalidParamsError(
            f"unknown params {unknown_params}: {NEW_ACTION_ID!r} only accepts {WORKDIR_PARAM!r}"
        )
    raw_workdir = params.get(WORKDIR_PARAM)
    if raw_workdir is None or raw_workdir == "":
        return None
    try:
        return Workdir(raw_workdir)
    except InvalidTerminalValueError as e:
        raise InvalidParamsError(f"invalid {WORKDIR_PARAM!r}: {e}") from e


@pure
def _session_name_for_key(key: InstanceKey) -> TmuxSessionName:
    """The key as a session name; a key that cannot be one (it carries a dot) names no terminal."""
    try:
        return TmuxSessionName(key)
    except InvalidTerminalValueError as e:
        raise UnknownInstanceError(f"no terminal has the key {key!r}") from e


@pure
def _session_name_for_title(title: InstanceTitle) -> TmuxSessionName:
    """The true name a title canonicalizes to; a title with nothing usable in it is a bad title."""
    canonical = canonical_name_from_title(title)
    if not canonical:
        raise InvalidInstanceValueError(
            f"invalid title {title!r}: it contains no usable characters"
        )
    try:
        return TmuxSessionName(canonical)
    except InvalidTerminalValueError as e:
        raise InvalidInstanceValueError(f"invalid title {title!r}: {e}") from e


class TmuxSessionSource(InstanceSourceInterface):
    """The terminal's instances: the user's tmux sessions, plus the terminals the store remembers that tmux no longer has.

    Keys are session names. A rename is therefore a re-key: tmux renames the session, the store
    swaps the record, and the tmux hook re-points the affected tabs.
    """

    tmux: TmuxInterface = Field(frozen=True, description="The default tmux server")
    store: TerminalSessionStoreInterface = Field(
        frozen=True, description="The app's own record of its terminals"
    )
    agent_session_prefix: str = Field(
        frozen=True,
        description="The prefix of mngr agents' sessions, which are never terminals",
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def list_instances(self) -> list[InstanceRecord]:
        with self._lock:
            live_sessions = self._user_sessions()
            records = self.store.list_records()
        return build_instance_records(live_sessions, records)

    def create_instance(
        self, action: ActionId, params: Mapping[str, str]
    ) -> InstanceRecord:
        if action != NEW_ACTION_ID:
            raise UnknownActionError(
                f"unknown action {action!r}: the terminal only declares {NEW_ACTION_ID!r}"
            )
        workdir = _parse_workdir(params)
        with self._lock:
            taken_names = self._taken_names()
            record = TerminalSessionRecord(
                name=TmuxSessionName(allocate_key(TERMINAL_KEY_PREFIX, taken_names)),
                title=None,
                workdir=workdir,
            )
            # The session itself is created on first attach (tmux new-session -A), as today.
            self.store.save_record(record)
        return _stopped_instance_record(record)

    def delete_instance(self, key: InstanceKey) -> None:
        try:
            name = _session_name_for_key(key)
        except UnknownInstanceError:
            # DELETE of an unknown key is a 204 by contract; a key no terminal can have is one.
            logger.debug("Ignored deleting {!r}: no terminal can have that key", key)
            return
        if is_agent_session(name, self.agent_session_prefix):
            raise InstanceConflictError(
                f"Refusing to destroy non-terminal session: {name!r}"
            )
        with self._lock:
            self.tmux.kill_session(name)
            self.store.remove_record(name)

    def rename_instance(self, key: InstanceKey, title: InstanceTitle) -> InstanceRecord:
        name = _session_name_for_key(key)
        new_name = _session_name_for_title(title)
        if is_agent_session(name, self.agent_session_prefix) or is_agent_session(
            new_name, self.agent_session_prefix
        ):
            raise InstanceConflictError(
                f"Refusing to rename to or from a non-terminal session: {name!r} -> {new_name!r}"
            )
        with self._lock:
            live_by_name = {session.name: session for session in self._user_sessions()}
            record_by_name = {
                record.name: record for record in self.store.list_records()
            }
            if name not in live_by_name and name not in record_by_name:
                raise UnknownInstanceError(f"no terminal has the key {key!r}")
            if new_name != name:
                other_names = (set(live_by_name) | set(record_by_name)) - {name}
                if is_name_conflict(title, other_names):
                    raise InstanceConflictError(
                        f"another terminal is already named {new_name!r}"
                    )
                if name in live_by_name:
                    self.tmux.rename_session(name, new_name)
            existing = record_by_name.get(name)
            renamed = TerminalSessionRecord(
                name=new_name,
                title=title,
                workdir=existing.workdir if existing else None,
            )
            self.store.replace_record(name, renamed)
            live_session = live_by_name.get(name)
        if live_session is None:
            return _stopped_instance_record(renamed)
        return _live_instance_record(
            live_session.model_copy_update(
                to_update(live_session.field_ref().name, str(new_name))
            ),
            renamed,
        )

    def set_location(self, key: InstanceKey, path: LocationPath) -> InstanceRecord:
        raise LocationNotTrackedError("the terminal does not track where its pages are")

    def _user_sessions(self) -> list[TmuxSession]:
        """The live sessions that are terminals: not an agent's, and named so the name can be a key."""
        user_sessions: list[TmuxSession] = []
        for session in self.tmux.list_sessions():
            if is_agent_session(session.name, self.agent_session_prefix):
                continue
            if not _is_session_name(session.name):
                logger.debug(
                    "Skipped tmux session {!r}: its name cannot be an instance key",
                    session.name,
                )
                continue
            user_sessions.append(session)
        return user_sessions

    def _taken_names(self) -> set[str]:
        return {session.name for session in self._user_sessions()} | {
            record.name for record in self.store.list_records()
        }


@pure
def _is_session_name(name: str) -> bool:
    try:
        TmuxSessionName(name)
    except InvalidTerminalValueError:
        return False
    return True
