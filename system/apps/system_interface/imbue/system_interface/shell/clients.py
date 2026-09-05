"""Client records: ``clients.json`` (contracts.md section 7), the active view and last-seen stamp per browser context."""

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
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import ViewId
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

CLIENTS_FILENAME: Final[str] = "clients.json"
CLIENTS_FILE_VERSION: Final[int] = 1

# A client unseen for this long is dropped, together with every layout it owns.
CLIENT_RETENTION: Final[timedelta] = timedelta(days=90)


class _StoredClient(FrozenModel):
    """One entry of the ``clients`` map (the id is the key)."""

    device_kind: DeviceKind = Field(description="Desktop or mobile")
    active_view: ViewId = Field(description="The view the client is on")
    last_seen: datetime = Field(description="When the client last reported")


class ClientsDocument(FrozenModel):
    """The whole of ``clients.json``."""

    version: int = Field(description="The file format version")
    clients: dict[str, _StoredClient] = Field(description="Every client, by id")


@pure
def client_wire_json(record: ClientRecord) -> dict[str, Any]:
    """The ``client`` object of contracts.md section 6."""
    return {
        "id": str(record.id),
        "device_kind": record.device_kind.value,
        "active_view": str(record.active_view),
        "last_seen": record.last_seen.isoformat(),
    }


class ClientStore(MutableModel):
    """Reads and writes ``clients.json`` under the shell's state lock."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _path(self) -> Path:
        return self.state_directory / CLIENTS_FILENAME

    def _read_unlocked(self) -> ClientsDocument:
        raw = read_json_object(self._path())
        if raw is None:
            return ClientsDocument(version=CLIENTS_FILE_VERSION, clients={})
        try:
            return ClientsDocument.model_validate(raw)
        except ValidationError as e:
            logger.warning("Ignored an unreadable clients file at {}: {}", self._path(), e.errors()[0]["msg"])
            return ClientsDocument(version=CLIENTS_FILE_VERSION, clients={})

    def _write_unlocked(self, document: ClientsDocument) -> None:
        write_json_atomic(self._path(), document.model_dump(mode="json"))

    def list_clients(self) -> list[ClientRecord]:
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
        records: list[ClientRecord] = []
        for client_id, stored in document.clients.items():
            try:
                records.append(
                    ClientRecord(
                        id=ClientId(client_id),
                        device_kind=stored.device_kind,
                        active_view=stored.active_view,
                        last_seen=stored.last_seen,
                    )
                )
            except (ValueError, ValidationError) as e:
                logger.warning("Skipped an unusable client record {!r}: {}", client_id, e)
        return sorted(records, key=lambda record: record.last_seen, reverse=True)

    def get_client(self, client_id: str) -> ClientRecord | None:
        for record in self.list_clients():
            if record.id == client_id:
                return record
        return None

    def record_report(self, report: ClientStateReport, now: datetime) -> ClientRecord:
        """Record a ``client_state`` report: the client's device kind, active view, and last-seen stamp."""
        stamped = now.astimezone(timezone.utc)
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            clients = {
                **document.clients,
                str(report.client_id): _StoredClient(
                    device_kind=report.device_kind, active_view=report.active_view, last_seen=stamped
                ),
            }
            self._write_unlocked(document.model_copy_update(to_update(document.field_ref().clients, clients)))
        return ClientRecord(
            id=report.client_id, device_kind=report.device_kind, active_view=report.active_view, last_seen=stamped
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
