"""The store's install records: one per app the store has installed or is working on, plus the job queue.

Persisted as one JSON document (``installs.json`` in the store's data folder), rewritten atomically on every change,
so a restart of the store finds exactly the state it left -- including an install it was in the middle of, which the
startup reconcile then cleans up.
"""

import threading
from datetime import UTC
from datetime import datetime
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from app_store.added_apps import AddedApp
from app_store.catalog import CatalogApp
from app_store.errors import StoreStateError
from getting_started.errors import StateFileError
from getting_started.state_files import read_json_object
from getting_started.state_files import write_json_atomic
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel


class InstallStatus(UpperCaseStrEnum):
    """Where an app is in its life in the store."""

    QUEUED = auto()
    DOWNLOADING = auto()
    CONFIGURING = auto()
    STARTING = auto()
    INSTALLED = auto()
    FAILED = auto()
    UNINSTALLING = auto()


# The statuses a job passes through while it runs; a record found in one at startup was interrupted.
IN_FLIGHT_STATUSES: Final[frozenset[InstallStatus]] = frozenset(
    {InstallStatus.DOWNLOADING, InstallStatus.CONFIGURING, InstallStatus.STARTING, InstallStatus.UNINSTALLING}
)


class JobKind(UpperCaseStrEnum):
    """What a queued job does."""

    # Install the app from its recipe.
    INSTALL = auto()
    # Rebuild an installed app's environment (it went missing, e.g. a restore that skips rebuildable folders),
    # keeping its data.
    REPAIR = auto()
    # Stop and remove the app.
    UNINSTALL = auto()
    # Replace an installed app with a newer version, keeping its data; the old version is put back if the new one
    # fails to install or start.
    UPDATE = auto()


class InstallRecord(FrozenModel):
    """One app the store has installed, is installing, or failed to install."""

    slug: str = Field(description="The catalog slug, which is also the installed app's name and program")
    version: str = Field(description="The version the recipe installs (or installed)")
    port: int | None = Field(default=None, description="The port it listens on, once one is picked")
    status: InstallStatus = Field(description="Where it is")
    job: JobKind | None = Field(default=None, description="The job queued or running for it; None when idle")
    is_deleting_data: bool = Field(default=False, description="For an uninstall: whether its data folder goes too")
    failure_reason: str = Field(default="", description="Why the last job failed, in plain words; empty otherwise")
    measured_disk_mb: int | None = Field(default=None, description="Disk its folder takes, measured after install")
    peak_memory_mb: int | None = Field(
        default=None,
        description="The most its process tree held during the first minutes of any run, measured; null until it ran",
    )
    update_to: CatalogApp | None = Field(
        default=None, description="For an update: the catalog entry of the version being installed"
    )
    update_entry: AddedApp | None = Field(
        default=None, description="For an update of an app the user added: its entry as it will be once updated"
    )
    updated_at: str = Field(description="When this record last changed, ISO 8601 UTC")


class StoreRecords(FrozenModel):
    """The whole document."""

    records: tuple[InstallRecord, ...] = Field(default=(), description="Every record, in the order first seen")
    queue: tuple[str, ...] = Field(
        default=(), description="Slugs with a job, oldest first; the head is the one running"
    )

    def find(self, slug: str) -> InstallRecord | None:
        for record in self.records:
            if record.slug == slug:
                return record
        return None


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class RecordStore(MutableModel):
    """The records as this process holds them, written through to disk on every change. Thread-safe."""

    path: Path = Field(frozen=True, description="The installs.json file")

    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)
    _records: StoreRecords | None = PrivateAttr(default=None)

    def read(self) -> StoreRecords:
        with self._lock:
            if self._records is None:
                self._records = self._load()
            return self._records

    def put(self, record: InstallRecord) -> InstallRecord:
        """Insert or replace the record for ``record.slug``."""
        with self._lock:
            current = self.read()
            records = [existing for existing in current.records if existing.slug != record.slug]
            position = next((i for i, existing in enumerate(current.records) if existing.slug == record.slug), None)
            records.insert(position if position is not None else len(records), record)
            self._save(current.model_copy(update={"records": tuple(records)}))
            return record

    def update(self, slug: str, **changes: object) -> InstallRecord:
        """Replace fields of the record for ``slug`` (stamping ``updated_at``) and return the new record."""
        with self._lock:
            record = self.read().find(slug)
            if record is None:
                raise StoreStateError(f"no install record for {slug}")
            updated = InstallRecord.model_validate({**record.model_dump(), **changes, "updated_at": now_iso()})
            return self.put(updated)

    def remove(self, slug: str) -> None:
        with self._lock:
            current = self.read()
            self._save(
                StoreRecords(
                    records=tuple(record for record in current.records if record.slug != slug),
                    queue=tuple(queued for queued in current.queue if queued != slug),
                )
            )

    def enqueue(self, slug: str) -> None:
        with self._lock:
            current = self.read()
            if slug not in current.queue:
                self._save(current.model_copy(update={"queue": (*current.queue, slug)}))

    def dequeue(self, slug: str) -> None:
        with self._lock:
            current = self.read()
            self._save(
                current.model_copy(update={"queue": tuple(queued for queued in current.queue if queued != slug)})
            )

    def _load(self) -> StoreRecords:
        document = read_json_object(self.path)
        if document is None:
            return StoreRecords()
        try:
            return StoreRecords.model_validate(document)
        except ValidationError as e:
            # An unreadable record set must not silently forget installed apps: refuse to start instead.
            raise StoreStateError(f"the install records at {self.path} do not validate: {e}") from e

    def _save(self, records: StoreRecords) -> None:
        try:
            write_json_atomic(self.path, records.model_dump(mode="json"))
        except StateFileError as e:
            logger.error("Could not save the install records: {}", e)
            raise StoreStateError(str(e)) from e
        self._records = records
