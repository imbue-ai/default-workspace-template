"""Client records: ``clients.json`` (desktop contracts.md section 4.3), the active desktop and last-seen stamp per browser context."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import ClientRecord
from imbue.system_interface.shell.data_types import ClientReportOutcome
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.errors import ClientNotFoundError
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

CLIENTS_FILENAME: Final[str] = "clients.json"
CLIENTS_FILE_VERSION: Final[int] = 2
# The tabbed shell's file: each client carried ``device_kind`` and ``active_view`` beside ``active_desktop``.
# CLEANUP: drop ``_LEGACY_CLIENTS_FILE_VERSION`` and ``_fold_legacy_client`` around late October 2026, once
# every workspace has written a version-2 clients.json (the first write after this release does).
_LEGACY_CLIENTS_FILE_VERSION: Final[int] = 1

# A client unseen for this long is dropped, together with every layout it owns.
CLIENT_RETENTION: Final[timedelta] = timedelta(days=90)


class _StoredClient(FrozenModel):
    """One entry of the ``clients`` map (the id is the key)."""

    active_desktop: DesktopId | None = Field(default=None, description="The desktop the client is on")
    last_seen: datetime = Field(description="When the client last reported")


class ClientsDocument(FrozenModel):
    """The whole of ``clients.json``."""

    version: int = Field(description="The file format version")
    clients: dict[str, _StoredClient] = Field(description="Every client, by id")


@pure
def client_wire_json(record: ClientRecord, is_connected: bool) -> dict[str, Any]:
    """The ``client`` object of desktop contracts.md section 5.5."""
    return {
        "id": str(record.id),
        "active_desktop": str(record.active_desktop) if record.active_desktop is not None else None,
        "last_seen": record.last_seen.isoformat(),
        "is_connected": is_connected,
    }


@pure
def _record_of(client_id: ClientId, stored: _StoredClient) -> ClientRecord:
    return ClientRecord(id=client_id, active_desktop=stored.active_desktop, last_seen=stored.last_seen)


@pure
def _fold_legacy_client(entry: Any) -> Any:
    """A version-1 entry as version 2 reads it: its desktop when it recorded one, else the view it was on (a desktop
    of that id, when one exists, is where the client lands; ``resolve_active_desktop`` falls back to the first
    desktop otherwise)."""
    if not isinstance(entry, dict):
        return entry
    desktop = entry.get("active_desktop") or entry.get("active_view")
    return {"active_desktop": desktop, "last_seen": entry.get("last_seen")}


class ClientStore(MutableModel):
    """Reads and writes ``clients.json`` under the shell's state lock."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _path(self) -> Path:
        return self.state_directory / CLIENTS_FILENAME

    def _read_unlocked(self) -> ClientsDocument:
        raw = read_json_object(self._path())
        if raw is None:
            return ClientsDocument(version=CLIENTS_FILE_VERSION, clients={})
        is_legacy = raw.get("version") == _LEGACY_CLIENTS_FILE_VERSION and isinstance(raw.get("clients"), dict)
        document_raw = (
            {
                "version": CLIENTS_FILE_VERSION,
                "clients": {client_id: _fold_legacy_client(entry) for client_id, entry in raw["clients"].items()},
            }
            if is_legacy
            else raw
        )
        try:
            document = ClientsDocument.model_validate(document_raw)
        except ValidationError as e:
            logger.warning("Ignored an unreadable clients file at {}: {}", self._path(), e.errors()[0]["msg"])
            return ClientsDocument(version=CLIENTS_FILE_VERSION, clients={})
        if document.version != CLIENTS_FILE_VERSION:
            logger.warning(
                "Ignored a clients file at {} of version {} (expected {})",
                self._path(),
                document.version,
                CLIENTS_FILE_VERSION,
            )
            return ClientsDocument(version=CLIENTS_FILE_VERSION, clients={})
        return document

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
        """Record a ``client_state`` report: the client's last-seen stamp and the desktop it names."""
        stamped = now.astimezone(timezone.utc)
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            previous = document.clients.get(str(report.client_id))
            stored = _StoredClient(active_desktop=report.active_desktop, last_seen=stamped)
            clients = {**document.clients, str(report.client_id): stored}
            self._write_unlocked(document.model_copy_update(to_update(document.field_ref().clients, clients)))
        previous_desktop = previous.active_desktop if previous is not None else None
        return ClientReportOutcome(
            record=_record_of(report.client_id, stored),
            is_active_desktop_changed=previous_desktop != report.active_desktop,
        )

    def set_active_desktop(self, client_id: ClientId, desktop_id: DesktopId, now: datetime) -> ClientReportOutcome:
        """Move a recorded client onto a desktop (a ``load`` op, an op's ``--desktop``, or a deleted desktop's
        fallback); raises ClientNotFoundError."""
        stamped = now.astimezone(timezone.utc)
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            previous = document.clients.get(str(client_id))
            if previous is None:
                raise ClientNotFoundError(f"No client record for {client_id!r}")
            updated = previous.model_copy_update(
                to_update(previous.field_ref().active_desktop, desktop_id),
                to_update(previous.field_ref().last_seen, stamped),
            )
            clients = {**document.clients, str(client_id): updated}
            self._write_unlocked(document.model_copy_update(to_update(document.field_ref().clients, clients)))
        return ClientReportOutcome(
            record=_record_of(client_id, updated),
            is_active_desktop_changed=previous.active_desktop != desktop_id,
        )

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
