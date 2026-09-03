import json
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from app_manifest.manifest import describe_validation_error
from app_manifest.primitives import ActionId, AppName
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from pydantic import Field, PrivateAttr, ValidationError

from app_instances.data_types import InstanceLifetime, InstanceRecord, InstanceStatus
from app_instances.errors import (
    InstanceConflictError,
    InstanceStoreError,
    InvalidParamsError,
    LocationNotTrackedError,
    NotRenameableError,
    UnknownActionError,
    UnknownInstanceError,
)
from app_instances.interfaces import InstanceSourceInterface
from app_instances.primitives import (
    InstanceKey,
    InstanceKeyPrefix,
    InstanceTitle,
    InstanceUrl,
    LocationPath,
    TitleTemplate,
)

# Where an app keeps its stored data (the workspace layout described in CLAUDE.md), relative
# to the repo root that every supervised program runs from.
APPS_DATA_DIR: Final[Path] = Path("data/.apps")
STORE_FILENAME: Final[str] = "instances.json"
STORE_VERSION: Final[int] = 1

# The one action the store implements, and the one parameter it accepts.
NEW_ACTION_ID: Final[ActionId] = ActionId("new")
PATH_PARAM: Final[str] = "path"
DEFAULT_PATH: Final[LocationPath] = LocationPath("/")

_KEY_NUMBER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<prefix>.+)-(?P<number>[1-9][0-9]*)$"
)


class _StoreDocument(FrozenModel):
    """The whole of an ``instances.json``."""

    version: int = Field(description="The document format version")
    instances: tuple[InstanceRecord, ...] = Field(
        description="Every stored instance, in creation order"
    )


@pure
def app_store_path(app_name: AppName) -> Path:
    return APPS_DATA_DIR / app_name / STORE_FILENAME


@pure
def instance_number(prefix: InstanceKeyPrefix, key: str) -> int | None:
    """The ``N`` of an allocated ``<prefix>-<N>`` key, or None for a key the allocator did not mint."""
    match = _KEY_NUMBER_PATTERN.match(key)
    if match is None or match.group("prefix") != prefix:
        return None
    return int(match.group("number"))


@pure
def allocate_key(
    prefix: InstanceKeyPrefix, taken_keys: AbstractSet[str]
) -> InstanceKey:
    """Mint the lowest free ``<prefix>-<N>`` (from 1), filling any gap a deletion left."""
    taken_numbers = {
        number
        for number in (instance_number(prefix, key) for key in taken_keys)
        if number is not None
    }
    number = 1
    while number in taken_numbers:
        number += 1
    return InstanceKey(f"{prefix}-{number}")


def current_utc_time() -> datetime:
    return datetime.now(timezone.utc)


class JsonStoreInstanceSource(InstanceSourceInterface):
    """Instances with no backing state of their own, kept as records in one JSON file.

    Each instance is a key and the path its page was last at. ``new`` allocates the lowest free
    ``<key_prefix>-<N>`` and stores ``params.path`` (default ``/``) as the URL; a location report
    replaces the URL and refreshes ``last_active``. The file is rewritten atomically (temp file
    plus rename) under a process-wide lock, so this process must be the file's only writer.
    """

    store_path: Path = Field(frozen=True, description="The instances.json file")
    key_prefix: InstanceKeyPrefix = Field(
        frozen=True, description="The prefix of allocated keys"
    )
    title_template: TitleTemplate = Field(
        frozen=True, description="The title of a new instance, with {n} for its number"
    )
    lifetime: InstanceLifetime = Field(
        frozen=True, description="The lifetime every stored instance reports"
    )
    is_renameable: bool = Field(frozen=True, description="Whether rename is accepted")
    is_location_tracked: bool = Field(
        frozen=True, description="Whether location reports are recorded"
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def list_instances(self) -> list[InstanceRecord]:
        with self._lock:
            return list(self._read_records())

    def create_instance(
        self, action: ActionId, params: Mapping[str, str]
    ) -> InstanceRecord:
        if action != NEW_ACTION_ID:
            raise UnknownActionError(
                f"unknown action {action!r}: this app only declares {NEW_ACTION_ID!r}"
            )
        unknown_params = sorted(set(params) - {PATH_PARAM})
        if unknown_params:
            raise InvalidParamsError(
                f"unknown params {unknown_params}: {NEW_ACTION_ID!r} only accepts {PATH_PARAM!r}"
            )
        path = LocationPath(params.get(PATH_PARAM, DEFAULT_PATH))
        with self._lock:
            records = self._read_records()
            key = allocate_key(self.key_prefix, {record.key for record in records})
            record = InstanceRecord(
                key=key,
                url=InstanceUrl(path),
                title=InstanceTitle(
                    self.title_template.replace(
                        "{n}", str(instance_number(self.key_prefix, key))
                    )
                ),
                status=InstanceStatus.IDLE,
                lifetime=self.lifetime,
                last_active=current_utc_time(),
                renameable=self.is_renameable,
            )
            self._write_records(records + (record,))
        return record

    def delete_instance(self, key: InstanceKey) -> None:
        with self._lock:
            records = self._read_records()
            remaining = tuple(record for record in records if record.key != key)
            if len(remaining) != len(records):
                self._write_records(remaining)

    def rename_instance(self, key: InstanceKey, title: InstanceTitle) -> InstanceRecord:
        if not self.is_renameable:
            raise NotRenameableError("this app's instances cannot be renamed")
        with self._lock:
            records = self._read_records()
            record = _find_record(records, key)
            if any(other.key != key and other.title == title for other in records):
                raise InstanceConflictError(
                    f"another instance is already titled {title!r}"
                )
            renamed = record.model_copy_update(
                to_update(record.field_ref().title, title)
            )
            self._write_records(_replace_record(records, renamed))
        return renamed

    def set_location(self, key: InstanceKey, path: LocationPath) -> InstanceRecord:
        if not self.is_location_tracked:
            raise LocationNotTrackedError(
                "this app does not track where its instances are"
            )
        with self._lock:
            records = self._read_records()
            record = _find_record(records, key)
            relocated = record.model_copy_update(
                to_update(record.field_ref().url, InstanceUrl(path)),
                to_update(record.field_ref().last_active, current_utc_time()),
            )
            self._write_records(_replace_record(records, relocated))
        return relocated

    def _read_records(self) -> tuple[InstanceRecord, ...]:
        """The stored records; a missing file is an empty store, an unreadable one raises InstanceStoreError."""
        if not self.store_path.exists():
            return ()
        try:
            raw_text = self.store_path.read_text(encoding="utf-8")
        except OSError as e:
            raise InstanceStoreError(
                f"cannot read the instance store {self.store_path}: {e}"
            ) from e
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as e:
            raise InstanceStoreError(
                f"the instance store {self.store_path} is not valid JSON: {e}"
            ) from e
        try:
            document = _StoreDocument.model_validate(data)
        except ValidationError as e:
            raise InstanceStoreError(
                f"the instance store {self.store_path} is malformed: {describe_validation_error(e)}"
            ) from e
        if document.version != STORE_VERSION:
            raise InstanceStoreError(
                f"the instance store {self.store_path} is version {document.version}; this library reads version {STORE_VERSION}"
            )
        return document.instances

    def _write_records(self, records: tuple[InstanceRecord, ...]) -> None:
        """Replace the file atomically: a reader sees the old document or the new one, never a partial write."""
        document = _StoreDocument(version=STORE_VERSION, instances=records)
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise InstanceStoreError(
                f"cannot create the instance store directory {self.store_path.parent}: {e}"
            ) from e
        temp_fd, temp_name = tempfile.mkstemp(
            dir=self.store_path.parent, prefix=f"{self.store_path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_file:
                json.dump(document.model_dump(mode="json"), temp_file, indent=2)
            os.replace(temp_name, self.store_path)
        except OSError as e:
            Path(temp_name).unlink(missing_ok=True)
            raise InstanceStoreError(
                f"cannot write the instance store {self.store_path}: {e}"
            ) from e


@pure
def _find_record(
    records: tuple[InstanceRecord, ...], key: InstanceKey
) -> InstanceRecord:
    for record in records:
        if record.key == key:
            return record
    raise UnknownInstanceError(f"no instance has the key {key!r}")


@pure
def _replace_record(
    records: tuple[InstanceRecord, ...], replacement: InstanceRecord
) -> tuple[InstanceRecord, ...]:
    return tuple(
        replacement if record.key == replacement.key else record for record in records
    )
