"""User records: ``users.json``, the desktop the shell made for each signed-in visitor (desktop plan section 3.10)."""

from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import UserRecord
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import UserId
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

USERS_FILENAME: Final[str] = "users.json"
USERS_FILE_VERSION: Final[int] = 1


class _StoredUser(FrozenModel):
    """One entry of the ``users`` map (the id is the key)."""

    desktop_id: DesktopId = Field(description="The desktop made for the user")
    desktop_name: str = Field(description="Its name as of the user's last arrival")
    email: str | None = Field(default=None, description="The verified email as of the last arrival")
    display_name: str | None = Field(default=None, description="The display name as of the last arrival")
    last_seen: datetime = Field(description="When a client of the user last arrived")


class UsersDocument(FrozenModel):
    """The whole of ``users.json``."""

    version: int = Field(description="The file format version")
    users: dict[str, _StoredUser] = Field(description="Every user, by id")


@pure
def _record_of(user_id: UserId, stored: _StoredUser) -> UserRecord:
    return UserRecord(
        user_id=user_id,
        desktop_id=stored.desktop_id,
        desktop_name=stored.desktop_name,
        email=stored.email,
        display_name=stored.display_name,
        last_seen=stored.last_seen,
    )


@pure
def _stored_of(record: UserRecord) -> _StoredUser:
    return _StoredUser(
        desktop_id=record.desktop_id,
        desktop_name=record.desktop_name,
        email=record.email,
        display_name=record.display_name,
        last_seen=record.last_seen.astimezone(timezone.utc),
    )


class UserStore(MutableModel):
    """Reads and writes ``users.json`` under the shell's state lock."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _path(self) -> Path:
        return self.state_directory / USERS_FILENAME

    def _read_unlocked(self) -> UsersDocument:
        raw = read_json_object(self._path())
        if raw is None:
            return UsersDocument(version=USERS_FILE_VERSION, users={})
        try:
            document = UsersDocument.model_validate(raw)
        except ValidationError as e:
            logger.warning("Ignored an unreadable users file at {}: {}", self._path(), e.errors()[0]["msg"])
            return UsersDocument(version=USERS_FILE_VERSION, users={})
        if document.version != USERS_FILE_VERSION:
            logger.warning(
                "Ignored a users file at {} of version {} (expected {})",
                self._path(),
                document.version,
                USERS_FILE_VERSION,
            )
            return UsersDocument(version=USERS_FILE_VERSION, users={})
        return document

    def _write_unlocked(self, document: UsersDocument) -> None:
        write_json_atomic(self._path(), document.model_dump(mode="json"))

    def list_users(self) -> list[UserRecord]:
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
        records: list[UserRecord] = []
        for user_id, stored in document.users.items():
            try:
                records.append(_record_of(UserId(user_id), stored))
            except (ValueError, ValidationError) as e:
                logger.warning("Skipped an unusable user record {!r}: {}", user_id, e)
        return records

    def get_user(self, user_id: UserId) -> UserRecord | None:
        for record in self.list_users():
            if record.user_id == user_id:
                return record
        return None

    def record_user(self, record: UserRecord) -> None:
        """Write (or replace) what the shell knows about one user."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            users = {**document.users, str(record.user_id): _stored_of(record)}
            self._write_unlocked(document.model_copy_update(to_update(document.field_ref().users, users)))
