"""Client records: ``clients.json`` (desktop contracts.md section 4.3), the active desktop, the last-seen stamp, and the
signed-in user, per browser context."""

from collections.abc import Callable
from collections.abc import Collection
from collections.abc import Mapping
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from app_manifest.primitives import AppName
from loguru import logger
from pydantic import Field
from pydantic import ValidationError
from workspace_layout.answers import ClientView
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import UserId
from workspace_layout.primitives import WindowId
from workspace_layout.records import ClientRecord
from workspace_layout.records import EntryPresentation

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import ClientReportOutcome
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.errors import ClientNotFoundError
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import parse_versioned_document
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

CLIENTS_FILENAME: Final[str] = "clients.json"
CLIENTS_FILE_VERSION: Final[int] = 2
# The tabbed shell's file: each client carried ``device_kind`` and ``active_view`` beside ``active_desktop``.
# CLEANUP: drop ``_LEGACY_CLIENTS_FILE_VERSION``, ``_fold_legacy_client``, ``_folded_if_legacy`` (reading the
# file straight through ``parse_versioned_document``), and clients_test's version-one test around late October
# 2026, once every workspace has written a version-2 clients.json (the first write after this release does).
_LEGACY_CLIENTS_FILE_VERSION: Final[int] = 1

# A client unseen for this long is dropped, together with every layout it owns.
CLIENT_RETENTION: Final[timedelta] = timedelta(days=90)

# The entry a client's shown history records for its home grid, beside the window ids it records for its windows.
SHOWN_HOME_ENTRY: Final[str] = "home"
# How many distinct entries a client's shown history keeps, the newest.
SHOWN_HISTORY_LIMIT: Final[int] = 20


class _StoredClient(FrozenModel):
    """One entry of the ``clients`` map (the id is the key)."""

    active_desktop: DesktopId | None = Field(default=None, description="The desktop the client is on")
    last_seen: datetime = Field(description="When the client last arrived or reported")
    user_id: UserId | None = Field(default=None, description="The signed-in visitor the client last arrived as")
    entries: dict[str, EntryPresentation] = Field(
        default_factory=dict, description="The client's presentation of each pinned entry, by app name"
    )
    shown_history: tuple[str, ...] = Field(
        default=(), description="What the client has shown on the phone layout, most recent last"
    )


class ClientsDocument(FrozenModel):
    """The whole of ``clients.json``."""

    version: int = Field(description="The file format version")
    clients: dict[str, _StoredClient] = Field(description="Every client, by id")


@pure
def entries_wire_json(entries: Mapping[str, EntryPresentation]) -> dict[str, Any]:
    """The ``entries`` map of the client object (pinned-taskbar-entries plan section 7.4)."""
    return {app: presentation.model_dump(mode="json") for app, presentation in entries.items()}


@pure
def client_view(record: ClientRecord, is_connected: bool) -> ClientView:
    """The ``client`` object of desktop contracts.md section 5.5."""
    return ClientView.model_validate({**dict(record), "is_connected": is_connected})


@pure
def _record_of(client_id: ClientId, stored: _StoredClient) -> ClientRecord:
    return ClientRecord(
        id=client_id,
        active_desktop=stored.active_desktop,
        last_seen=stored.last_seen,
        user_id=stored.user_id,
        entries=stored.entries,
        shown_history=stored.shown_history,
    )


@pure
def with_shown_entry(history: tuple[str, ...], entry: str) -> tuple[str, ...]:
    """The history with ``entry`` moved to its end (an earlier occurrence dropped), holding the newest
    ``SHOWN_HISTORY_LIMIT`` entries."""
    return (*(kept for kept in history if kept != entry), entry)[-SHOWN_HISTORY_LIMIT:]


@pure
def _fold_legacy_client(entry: Any) -> Any:
    """A version-1 entry as version 2 reads it: its desktop when it recorded one, else the view it was on (a desktop
    of that id, when one exists, is where the client lands; ``resolve_active_desktop`` falls back to the first
    desktop otherwise)."""
    if not isinstance(entry, dict):
        return entry
    desktop = entry.get("active_desktop") or entry.get("active_view")
    return {"active_desktop": desktop, "last_seen": entry.get("last_seen")}


@pure
def _folded_if_legacy(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """A version-1 document as a version-2 one, entry by entry; any other document (or none) as it is."""
    if raw is None or raw.get("version") != _LEGACY_CLIENTS_FILE_VERSION or not isinstance(raw.get("clients"), dict):
        return raw
    return {
        "version": CLIENTS_FILE_VERSION,
        "clients": {client_id: _fold_legacy_client(entry) for client_id, entry in raw["clients"].items()},
    }


@pure
def _moved_client(
    client_id: ClientId, previous: _StoredClient | None, desktop_id: DesktopId, stamped: datetime
) -> _StoredClient:
    """The recorded client on the desktop, stamped; a client with no record raises ClientNotFoundError."""
    if previous is None:
        raise ClientNotFoundError(f"No client record for {client_id!r}")
    return previous.model_copy_update(
        to_update(previous.field_ref().active_desktop, desktop_id),
        to_update(previous.field_ref().last_seen, stamped),
    )


class ClientStore(MutableModel):
    """Reads and writes ``clients.json`` under the shell's state lock."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _path(self) -> Path:
        return self.state_directory / CLIENTS_FILENAME

    def _read_unlocked(self) -> ClientsDocument:
        document = parse_versioned_document(
            _folded_if_legacy(read_json_object(self._path())), ClientsDocument, CLIENTS_FILE_VERSION, self._path()
        )
        return document if document is not None else ClientsDocument(version=CLIENTS_FILE_VERSION, clients={})

    def _write_unlocked(self, document: ClientsDocument) -> None:
        write_json_atomic(self._path(), document.model_dump(mode="json"))

    def list_clients(self) -> list[ClientRecord]:
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
        records: list[ClientRecord] = []
        for client_id, stored in document.clients.items():
            try:
                records.append(_record_of(ClientId(client_id), stored))
            except (ValueError, ValidationError) as e:
                logger.warning("Skipped an unusable client record {!r}: {}", client_id, e)
        return sorted(records, key=lambda record: record.last_seen, reverse=True)

    def get_client(self, client_id: str) -> ClientRecord | None:
        for record in self.list_clients():
            if record.id == client_id:
                return record
        return None

    def record_report(self, report: ClientStateReport, now: datetime) -> ClientReportOutcome:
        """Record a ``client_state`` report: the client's last-seen stamp and the desktop it names; the user it last
        arrived as stays."""
        stamped = now.astimezone(timezone.utc)
        return self._store_client(
            report.client_id,
            lambda previous: _StoredClient(
                active_desktop=report.active_desktop,
                last_seen=stamped,
                user_id=previous.user_id if previous is not None else None,
                entries=previous.entries if previous is not None else {},
                shown_history=previous.shown_history if previous is not None else (),
            ),
        )

    def set_active_desktop(self, client_id: ClientId, desktop_id: DesktopId, now: datetime) -> ClientReportOutcome:
        """Move a recorded client onto a desktop (a ``load`` op, an op's ``--desktop``, or a deleted desktop's
        fallback); raises ClientNotFoundError."""
        stamped = now.astimezone(timezone.utc)
        return self._store_client(client_id, lambda previous: _moved_client(client_id, previous, desktop_id, stamped))

    def record_arrival(
        self, client_id: ClientId, user_id: UserId | None, desktop_id: DesktopId, now: datetime
    ) -> ClientReportOutcome:
        """Record a client's arrival (the shell page loading): the user it arrived as and the desktop it lands on."""
        stamped = now.astimezone(timezone.utc)
        return self._store_client(
            client_id,
            lambda previous: _StoredClient(
                active_desktop=desktop_id,
                last_seen=stamped,
                user_id=user_id,
                entries=previous.entries if previous is not None else {},
                shown_history=previous.shown_history if previous is not None else (),
            ),
        )

    def _store_client(
        self, client_id: ClientId, build: Callable[[_StoredClient | None], _StoredClient]
    ) -> ClientReportOutcome:
        """Replace one client's entry with what ``build`` makes of the previous one (None for a new client), and
        answer whether the stored desktop moved."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            previous = document.clients.get(str(client_id))
            stored = build(previous)
            clients = {**document.clients, str(client_id): stored}
            self._write_unlocked(document.model_copy_update(to_update(document.field_ref().clients, clients)))
        previous_desktop = previous.active_desktop if previous is not None else None
        return ClientReportOutcome(
            record=_record_of(client_id, stored), is_active_desktop_changed=previous_desktop != stored.active_desktop
        )

    def _update_recorded_client(
        self, client_id: ClientId, update: Callable[[_StoredClient], _StoredClient]
    ) -> ClientRecord:
        """Replace a recorded client's entry with what ``update`` makes of it; raises ClientNotFoundError."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            previous = document.clients.get(str(client_id))
            if previous is None:
                raise ClientNotFoundError(f"No client record for {client_id!r}")
            updated = update(previous)
            clients = {**document.clients, str(client_id): updated}
            self._write_unlocked(document.model_copy_update(to_update(document.field_ref().clients, clients)))
        return _record_of(client_id, updated)

    def set_entry_presentation(
        self, client_id: ClientId, app: AppName, presentation: EntryPresentation, now: datetime
    ) -> ClientRecord:
        """Store how a recorded client shows one pinned entry; raises ClientNotFoundError."""
        stamped = now.astimezone(timezone.utc)
        return self._update_recorded_client(
            client_id,
            lambda previous: previous.model_copy_update(
                to_update(previous.field_ref().entries, {**previous.entries, str(app): presentation}),
                to_update(previous.field_ref().last_seen, stamped),
            ),
        )

    def record_shown(self, client_id: ClientId, window_id: WindowId | None, now: datetime) -> ClientRecord:
        """Record what a recorded client now shows (a window, or its home grid for None) as the newest entry of its
        shown history; raises ClientNotFoundError."""
        entry = SHOWN_HOME_ENTRY if window_id is None else str(window_id)
        stamped = now.astimezone(timezone.utc)
        return self._update_recorded_client(
            client_id,
            lambda previous: previous.model_copy_update(
                to_update(previous.field_ref().shown_history, with_shown_entry(previous.shown_history, entry)),
                to_update(previous.field_ref().last_seen, stamped),
            ),
        )

    def drop_windows(self, window_ids: Collection[WindowId]) -> None:
        """Drop closed windows from every client's shown history; writes only when a history named one."""
        closed = {str(window_id) for window_id in window_ids}
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            clients: dict[str, _StoredClient] = {}
            is_changed = False
            for client_id, stored in document.clients.items():
                kept = tuple(entry for entry in stored.shown_history if entry not in closed)
                if kept == stored.shown_history:
                    clients[client_id] = stored
                    continue
                clients[client_id] = stored.model_copy_update(to_update(stored.field_ref().shown_history, kept))
                is_changed = True
            if is_changed:
                self._write_unlocked(document.model_copy_update(to_update(document.field_ref().clients, clients)))

    def prune_unseen(self, now: datetime) -> list[ClientId]:
        """Drop every client unseen for the retention period; returns their ids so the caller can drop their layouts."""
        cutoff = now.astimezone(timezone.utc) - CLIENT_RETENTION
        pruned: list[ClientId] = []
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            kept: dict[str, _StoredClient] = {}
            for client_id, stored in document.clients.items():
                last_seen = (
                    stored.last_seen
                    if stored.last_seen.tzinfo is not None
                    else stored.last_seen.replace(tzinfo=timezone.utc)
                )
                if last_seen < cutoff:
                    pruned.append(ClientId(client_id))
                else:
                    kept[client_id] = stored
            if pruned:
                self._write_unlocked(document.model_copy_update(to_update(document.field_ref().clients, kept)))
        return pruned
