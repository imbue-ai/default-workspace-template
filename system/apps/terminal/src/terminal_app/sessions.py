import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
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
    LocationTarget,
    canonical_name_from_title,
    is_name_conflict,
)
from app_manifest.primitives import ActionId
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field, PrivateAttr

from terminal_app.data_types import TerminalSessionRecord, TmuxSession
from terminal_app.errors import InvalidTerminalValueError, TmuxCommandError
from terminal_app.interfaces import TerminalSessionStoreInterface, TmuxInterface
from terminal_app.primitives import (
    TmuxSessionId,
    TmuxSessionName,
    Workdir,
    derive_terminal_title,
    instance_url_for_session,
)

# The names the ``new`` action mints: the lowest free ``terminal-<N>``.
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
def is_same_session(record: TerminalSessionRecord, session: TmuxSession) -> bool:
    """Whether the live session is the one the record remembers: the same id, created at the same time.

    tmux hands ids out afresh on every server, so a record's id alone would bind it to whatever
    session a later server gave that number; the creation time tells the two apart. A side that
    knows no creation time (a record from before it was kept, a tmux that printed none) matches
    on the id alone.
    """
    if record.session_id is None or record.session_id != session.session_id:
        return False
    if record.session_created is None or session.created_epoch is None:
        return True
    return record.session_created == session.created_epoch


@pure
def _find_record_of_session(
    session: TmuxSession, records: Sequence[TerminalSessionRecord]
) -> TerminalSessionRecord | None:
    return next((record for record in records if is_same_session(record, session)), None)


@pure
def _find_session_of_record(
    record: TerminalSessionRecord, live_sessions: Sequence[TmuxSession]
) -> TmuxSession | None:
    return next((session for session in live_sessions if is_same_session(record, session)), None)


@pure
def _unclaimed_session_named(
    name: TmuxSessionName,
    live_sessions: Sequence[TmuxSession],
    records: Sequence[TerminalSessionRecord],
) -> TmuxSession | None:
    """The live session tmux lists under ``name``, unless a terminal already holds it by id.

    A session renamed inside tmux to another terminal's key stays the terminal that holds its
    id; its name is not a second way to claim it, or two terminals would share one session and
    stopping either would kill the other's.
    """
    return next(
        (
            session
            for session in live_sessions
            if session.name == name and _find_record_of_session(session, records) is None
        ),
        None,
    )


@pure
def _live_instance_record(
    session: TmuxSession, record: TerminalSessionRecord | None
) -> InstanceRecord:
    name = record.name if record is not None else TmuxSessionName(session.name)
    return InstanceRecord(
        key=InstanceKey(name),
        url=instance_url_for_session(name, record.workdir if record else None),
        title=record.title if record and record.title else derive_terminal_title(name),
        status=InstanceStatus.IDLE,
        lifetime=InstanceLifetime.EXPLICIT,
        last_active=session.last_activity,
        renameable=True,
        stoppable=True,
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
        stoppable=True,
    )


@pure
def _fresh_instance_record(record: TerminalSessionRecord) -> InstanceRecord:
    """The idle record of a terminal whose session was just created, so has seen no activity."""
    if record.session_id is None:
        raise InvalidTerminalValueError(
            f"terminal {record.name!r} has no session id although its session was just created"
        )
    return _live_instance_record(
        TmuxSession(
            name=record.name,
            session_id=record.session_id,
            created_epoch=record.session_created,
            last_activity=None,
        ),
        record,
    )


@pure
def match_live_sessions(
    live_sessions: Sequence[TmuxSession], records: Sequence[TerminalSessionRecord]
) -> list[InstanceRecord]:
    """Every live user session as ``idle`` (in tmux order), then every remembered terminal tmux no longer has as ``stopped``.

    A live session is the terminal whose record holds its id, whatever tmux now calls it; a
    session no record holds by id falls back to the record of its name when that record's own
    session is not live (one from before the app kept ids, or a session created on attach), and
    one with no record at all is a hand-made terminal listed under its own name. A session
    carrying the old name of a terminal whose own session is live is skipped, whichever tmux
    lists first.
    """
    # Keyed by a plain string: a live session's name arrives from tmux unvalidated.
    record_by_name: dict[str, TerminalSessionRecord] = {
        str(record.name): record for record in records
    }
    matched: set[TmuxSessionName] = set()
    instances: list[InstanceRecord] = []
    for session in live_sessions:
        record = _find_record_of_session(session, records)
        if record is None:
            record = record_by_name.get(session.name)
            if record is not None and _find_session_of_record(record, live_sessions) is not None:
                # The record's own session is live under another name; this one only reuses its old name.
                logger.debug(
                    "Skipped tmux session {!r} ({}): terminal {!r} is backed by session {}",
                    session.name,
                    session.session_id,
                    record.name,
                    record.session_id,
                )
                continue
        if record is not None:
            matched.add(record.name)
        instances.append(_live_instance_record(session, record))
    instances.extend(
        _stopped_instance_record(record) for record in records if record.name not in matched
    )
    return instances


@pure
def _parse_workdir(params: Mapping[str, str], default_workdir: Workdir) -> Workdir:
    unknown_params = sorted(set(params) - {WORKDIR_PARAM})
    if unknown_params:
        raise InvalidParamsError(
            f"unknown params {unknown_params}: {NEW_ACTION_ID!r} only accepts {WORKDIR_PARAM!r}"
        )
    raw_workdir = params.get(WORKDIR_PARAM)
    if raw_workdir is None or raw_workdir == "":
        return default_workdir
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
def _require_usable_title(title: InstanceTitle) -> None:
    """A title must canonicalize to something a name could be made of; one that does not is a bad title."""
    canonical = canonical_name_from_title(title)
    if not canonical:
        raise InvalidInstanceValueError(
            f"invalid title {title!r}: it contains no usable characters"
        )
    try:
        TmuxSessionName(canonical)
    except InvalidTerminalValueError as e:
        raise InvalidInstanceValueError(f"invalid title {title!r}: {e}") from e


@pure
def _is_session_name(name: str) -> bool:
    try:
        TmuxSessionName(name)
    except InvalidTerminalValueError:
        return False
    return True


class TmuxSessionSource(InstanceSourceInterface):
    """The terminal's instances: the user's tmux sessions, plus the terminals the store remembers that tmux no longer has.

    Keys are the names the app allocated (``terminal-<N>``) and never change: a record is matched
    to its live session by tmux's session id and the session's creation time (``is_same_session``),
    a rename changes only the record's title, and the session id and creation time of every
    terminal the app created or adopted are written under ``sessions_dir`` for the dispatch
    script to attach by.
    """

    tmux: TmuxInterface = Field(frozen=True, description="The default tmux server")
    store: TerminalSessionStoreInterface = Field(
        frozen=True, description="The app's own record of its terminals"
    )
    agent_session_prefix: str = Field(
        frozen=True,
        description="The prefix of mngr agents' sessions, which are never terminals",
    )
    # A create that names no directory starts its shell here: the workspace root under
    # supervisord, rather than wherever the dispatch script would fall back to.
    default_workdir: Workdir = Field(
        frozen=True, description="Where a terminal created without a workdir starts"
    )
    sessions_dir: Path = Field(
        frozen=True,
        description="Where the session id and creation time of each terminal are written, named by key, for the dispatch to attach by",
    )
    session_command: tuple[str, ...] = Field(
        frozen=True,
        description="The command a new session runs: the login shell behind the memory-shedding tag",
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def list_instances(self) -> list[InstanceRecord]:
        with self._lock:
            live_sessions = self._user_sessions()
            records = self.store.list_records()
        return match_live_sessions(live_sessions, records)

    def create_instance(
        self, action: ActionId, params: Mapping[str, str]
    ) -> InstanceRecord:
        if action != NEW_ACTION_ID:
            raise UnknownActionError(
                f"unknown action {action!r}: the terminal only declares {NEW_ACTION_ID!r}"
            )
        workdir = _parse_workdir(params, self.default_workdir)
        with self._lock:
            taken_names = self._taken_names()
            name = TmuxSessionName(allocate_key(TERMINAL_KEY_PREFIX, taken_names))
            record = self._create_session_for(
                TerminalSessionRecord(name=name, title=None, workdir=workdir)
            )
        return _fresh_instance_record(record)

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
            record = self._record_named(name)
            live = self._live_session_of(name, record, self._user_sessions())
            if live is not None:
                self.tmux.kill_session(TmuxSessionId(live.session_id))
            self.store.remove_record(name)
            self._remove_session_id_file(name)

    def rename_instance(self, key: InstanceKey, title: InstanceTitle) -> InstanceRecord:
        name = _session_name_for_key(key)
        if is_agent_session(name, self.agent_session_prefix):
            raise InstanceConflictError(
                f"Refusing to rename a non-terminal session: {name!r}"
            )
        _require_usable_title(title)
        with self._lock:
            live_sessions = self._user_sessions()
            records = self.store.list_records()
            listed = match_live_sessions(live_sessions, records)
            if not any(instance.key == name for instance in listed):
                raise UnknownInstanceError(f"no terminal has the key {key!r}")
            other_titles = [str(instance.title) for instance in listed if instance.key != name]
            if is_name_conflict(title, other_titles):
                raise InstanceConflictError(
                    f"another terminal is already named {canonical_name_from_title(title)!r}"
                )
            existing = next((record for record in records if record.name == name), None)
            live_session = self._live_session_of(name, existing, live_sessions)
            if existing is None:
                existing = TerminalSessionRecord(name=name, title=None, workdir=None)
            retitled = existing.model_copy_update(to_update(existing.field_ref().title, title))
            if live_session is not None and not is_same_session(retitled, live_session):
                # The live session of its name is not the one the record holds (a session the
                # dispatch created on attach, or one the store never saw): it is the terminal's now.
                retitled = self._bind(retitled, live_session)
            else:
                self.store.save_record(retitled)
        if live_session is None:
            return _stopped_instance_record(retitled)
        return _live_instance_record(live_session, retitled)

    def set_location(self, key: InstanceKey, path: LocationTarget) -> InstanceRecord:
        raise LocationNotTrackedError("the terminal does not track where its pages are")

    def stop_instance(self, key: InstanceKey) -> InstanceRecord:
        """Kill the terminal's session and remember it as stopped, so neither a startup nor a start brings it back unasked."""
        name = self._terminal_name_or_raise(key, "stop")
        with self._lock:
            live_sessions = self._user_sessions()
            record = self._record_named(name)
            live = self._live_session_of(name, record, live_sessions)
            if record is None and live is None:
                raise UnknownInstanceError(f"no terminal has the key {key!r}")
            if live is not None:
                self.tmux.kill_session(TmuxSessionId(live.session_id))
            stopped = TerminalSessionRecord(
                name=name,
                title=record.title if record else None,
                workdir=record.workdir if record else None,
                session_id=None,
                session_created=None,
                is_stopped=True,
            )
            self.store.save_record(stopped)
            self._remove_session_id_file(name)
        return _stopped_instance_record(stopped)

    def start_instance(self, key: InstanceKey) -> InstanceRecord:
        """Recreate a stopped terminal's session in its workdir; a running terminal is left as it is."""
        name = self._terminal_name_or_raise(key, "start")
        with self._lock:
            live_sessions = self._user_sessions()
            record = self._record_named(name)
            live = self._live_session_of(name, record, live_sessions)
            if live is not None:
                if record is not None and not is_same_session(record, live):
                    self._adopt(record, live)
                return _live_instance_record(live, record)
            if record is None:
                raise UnknownInstanceError(f"no terminal has the key {key!r}")
            try:
                created = self._create_session_for(record)
            except TmuxCommandError as e:
                # tmux refuses a name a session already carries: one renamed inside tmux to this
                # terminal's key, which belongs to the terminal holding its id, not to this one.
                raise InstanceConflictError(
                    f"could not start terminal {name!r}: {e}"
                ) from e
        return _fresh_instance_record(created)

    def _terminal_name_or_raise(self, key: InstanceKey, verb: str) -> TmuxSessionName:
        name = _session_name_for_key(key)
        if is_agent_session(name, self.agent_session_prefix):
            raise InstanceConflictError(
                f"Refusing to {verb} a non-terminal session: {name!r}"
            )
        return name

    def recreate_remembered_sessions(self) -> None:
        """Bring every remembered terminal back at startup: a live session is adopted, a lost one is recreated unless the user stopped it.

        A record whose session tmux still has (the app restarted, the server did not) gets its
        id file rewritten; one tmux lost (a container restart) gets a fresh session, so a
        terminal never shows as stopped merely because the workspace was restarted. A failure
        to create one is logged and leaves that terminal stopped until it is started or opened.
        """
        with self._lock:
            live_sessions = self._user_sessions()
            records = self.store.list_records()
            for record in records:
                own = _find_session_of_record(record, live_sessions)
                if own is not None:
                    if record.session_created == own.created_epoch:
                        self._write_session_id_file(record)
                    else:
                        # A record from before creation times were kept matched on the id alone;
                        # it learns the time here, since the dispatch attaches only by both.
                        self._bind(record, own)
                    continue
                live = _unclaimed_session_named(record.name, live_sessions, records)
                if live is not None:
                    self._adopt(record, live)
                    continue
                if record.is_stopped:
                    self._remove_session_id_file(record.name)
                    continue
                try:
                    self._create_session_for(record)
                except TmuxCommandError as e:
                    logger.warning(
                        "Could not recreate the session of terminal {}: {}", record.name, e
                    )

    def observe_attached_session(
        self, session_id: str, session_name: str
    ) -> TmuxSessionName | None:
        """The key of the session a client now shows, adopting it when a record of that name has no live session yet.

        None for an agent's session, a name that cannot be a key, or a session under the old
        name of a terminal whose own session is live (no terminal, as the listing has it). A
        record without an id (from before the app kept them) or whose session was recreated on
        attach takes the live session's id, and is no longer stopped.
        """
        if is_agent_session(session_name, self.agent_session_prefix):
            return None
        with self._lock:
            live_sessions = self._user_sessions()
            live = next((session for session in live_sessions if session.session_id == session_id), None)
            if live is None:
                # The session is gone already, or is one the listing skips (a name that cannot be a key).
                return None
            records = self.store.list_records()
            record = _find_record_of_session(live, records)
            if record is not None:
                return record.name
            if not _is_session_name(session_name):
                return None
            name = TmuxSessionName(session_name)
            record = next((record for record in records if record.name == name), None)
            if record is None:
                return name
            if _find_session_of_record(record, live_sessions) is not None:
                logger.debug(
                    "Ignored tmux session {!r} ({}): terminal {!r} is backed by session {}",
                    session_name,
                    session_id,
                    name,
                    record.session_id,
                )
                return None
            self._adopt(record, live)
            return name

    def _create_session_for(self, record: TerminalSessionRecord) -> TerminalSessionRecord:
        """Create the record's session, then remember it with the new id and its id file written; the caller holds the lock."""
        session = self.tmux.create_session(
            record.name, record.workdir or self.default_workdir, self.session_command
        )
        return self._bind(record, session)

    def _adopt(self, record: TerminalSessionRecord, session: TmuxSession) -> TerminalSessionRecord:
        """Bind a record to the live session that carries its name; the caller holds the lock."""
        return self._bind(record, session)

    def _bind(self, record: TerminalSessionRecord, session: TmuxSession) -> TerminalSessionRecord:
        """Remember ``session`` as the record's, running, with its id file written."""
        bound = record.model_copy_update(
            to_update(record.field_ref().session_id, TmuxSessionId(session.session_id)),
            to_update(record.field_ref().session_created, session.created_epoch),
            to_update(record.field_ref().is_stopped, False),
        )
        self.store.save_record(bound)
        self._write_session_id_file(bound)
        return bound

    def _record_named(self, name: TmuxSessionName) -> TerminalSessionRecord | None:
        return next(
            (record for record in self.store.list_records() if record.name == name), None
        )

    def _live_session_of(
        self,
        name: TmuxSessionName,
        record: TerminalSessionRecord | None,
        live_sessions: Sequence[TmuxSession],
    ) -> TmuxSession | None:
        """The live session backing the terminal: the record's own session, else the one with its name that no terminal holds by id."""
        if record is not None:
            own = _find_session_of_record(record, live_sessions)
            if own is not None:
                return own
        return _unclaimed_session_named(name, live_sessions, self.store.list_records())

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

    def _session_id_file(self, name: TmuxSessionName) -> Path:
        return self.sessions_dir / name

    def _write_session_id_file(self, record: TerminalSessionRecord) -> None:
        """The two lines the dispatch attaches by: the session id, then its creation time (blank when unknown)."""
        if record.session_id is None:
            raise InvalidTerminalValueError(
                f"terminal {record.name!r} has no session id to write for the dispatch"
            )
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        created = "" if record.session_created is None else str(record.session_created)
        self._session_id_file(record.name).write_text(f"{record.session_id}\n{created}\n")

    def _remove_session_id_file(self, name: TmuxSessionName) -> None:
        self._session_id_file(name).unlink(missing_ok=True)
