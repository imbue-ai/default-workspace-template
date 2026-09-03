import threading
from pathlib import Path
from typing import Final

from app_instances.errors import InstanceStoreError
from app_instances.json_store import read_json_document, write_json_document
from pydantic import Field, PrivateAttr

from terminal_app.data_types import TerminalSessionRecord, TerminalStoreDocument
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
            others = tuple(
                existing for existing in self._read() if existing.name != record.name
            )
            self._write(others + (record,))

    def replace_record(
        self, name: TmuxSessionName, record: TerminalSessionRecord
    ) -> None:
        with self._lock:
            others = tuple(
                existing
                for existing in self._read()
                if existing.name not in (name, record.name)
            )
            self._write(others + (record,))

    def remove_record(self, name: TmuxSessionName) -> None:
        with self._lock:
            records = self._read()
            remaining = tuple(existing for existing in records if existing.name != name)
            if len(remaining) != len(records):
                self._write(remaining)

    def _read(self) -> tuple[TerminalSessionRecord, ...]:
        document = read_json_document(self.store_path, TerminalStoreDocument)
        if document is None:
            return ()
        if document.version != STORE_VERSION:
            raise InstanceStoreError(
                f"the terminal store {self.store_path} is version {document.version}; this app reads version {STORE_VERSION}"
            )
        return document.sessions

    def _write(self, records: tuple[TerminalSessionRecord, ...]) -> None:
        write_json_document(
            self.store_path,
            TerminalStoreDocument(version=STORE_VERSION, sessions=records),
        )
