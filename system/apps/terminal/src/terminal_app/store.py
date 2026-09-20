import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Final

from app_manifest.manifest import describe_validation_error
from imbue.imbue_common.pure import pure
from pydantic import Field, PrivateAttr, ValidationError

from terminal_app.data_types import TerminalSessionRecord, TerminalStoreDocument
from terminal_app.errors import TerminalStoreError
from terminal_app.interfaces import TerminalSessionStoreInterface
from terminal_app.primitives import TmuxSessionName

STORE_VERSION: Final[int] = 1


class JsonTerminalSessionStore(TerminalSessionStoreInterface):
    """The terminal records in one JSON file, rewritten atomically under the store's own lock.

    One store per process must be the file's only writer. A file that will not read raises
    rather than reading as empty, so a corrupt store never silently forgets every terminal.
    """

    store_path: Path = Field(frozen=True, description="The instances.json file")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def list_records(self) -> list[TerminalSessionRecord]:
        with self._lock:
            return list(self._read())

    def save_record(self, record: TerminalSessionRecord) -> None:
        with self._lock:
            self._write(_with_record(self._read(), record))

    def remove_record(self, name: TmuxSessionName) -> None:
        with self._lock:
            records = self._read()
            remaining = tuple(existing for existing in records if existing.name != name)
            if len(remaining) != len(records):
                self._write(remaining)

    def _read(self) -> tuple[TerminalSessionRecord, ...]:
        document = _read_store_document(self.store_path)
        if document is None:
            return ()
        if document.version != STORE_VERSION:
            raise TerminalStoreError(
                f"the terminal store {self.store_path} is version {document.version}; this app reads version {STORE_VERSION}"
            )
        return document.sessions

    def _write(self, records: tuple[TerminalSessionRecord, ...]) -> None:
        _write_store_document(
            self.store_path,
            TerminalStoreDocument(version=STORE_VERSION, sessions=records),
        )


def _read_store_document(path: Path) -> TerminalStoreDocument | None:
    """The store at ``path``, or None when there is no file; a file that cannot be read, is not JSON, or does not
    fit the document raises TerminalStoreError rather than reading as empty."""
    if not path.exists():
        return None
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise TerminalStoreError(f"cannot read the terminal store {path}: {e}") from e
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise TerminalStoreError(f"the terminal store {path} is not valid JSON: {e}") from e
    try:
        return TerminalStoreDocument.model_validate(data)
    except ValidationError as e:
        raise TerminalStoreError(
            f"the terminal store {path} is malformed: {describe_validation_error(e)}"
        ) from e


def _write_store_document(path: Path, document: TerminalStoreDocument) -> None:
    """Replace the file at ``path`` atomically: a reader sees the old document or the new one, never a partial write."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise TerminalStoreError(f"cannot create the terminal store directory {path.parent}: {e}") from e
    try:
        temp_fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    except OSError as e:
        raise TerminalStoreError(f"cannot create a temporary file beside the terminal store {path}: {e}") from e
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_file:
            json.dump(document.model_dump(mode="json"), temp_file, indent=2)
        os.replace(temp_name, path)
    except OSError as e:
        Path(temp_name).unlink(missing_ok=True)
        raise TerminalStoreError(f"cannot write the terminal store {path}: {e}") from e


@pure
def _with_record(
    records: tuple[TerminalSessionRecord, ...], record: TerminalSessionRecord
) -> tuple[TerminalSessionRecord, ...]:
    """``records`` with the one of the same name replaced in place by ``record`` (appended when there is none)."""
    if not any(existing.name == record.name for existing in records):
        return records + (record,)
    return tuple(
        record if existing.name == record.name else existing for existing in records
    )
