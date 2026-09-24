import json
from datetime import timedelta
from pathlib import Path

from imbue.imbue_common.model_update import to_update
from imbue.system_interface.shell.data_types import UserRecord
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import UserId
from imbue.system_interface.shell.testing import TEST_NOW
from imbue.system_interface.shell.users import USERS_FILENAME
from imbue.system_interface.shell.users import UserStore


def _record(user_id: str, desktop_id: str, display_name: str | None = None) -> UserRecord:
    return UserRecord(
        user_id=UserId(user_id),
        desktop_id=DesktopId(desktop_id),
        desktop_name=desktop_id.title(),
        email=f"{user_id}@example.com",
        display_name=display_name,
        last_seen=TEST_NOW,
    )


def test_users_are_recorded_replaced_and_read_back(tmp_path: Path) -> None:
    store = UserStore(state_directory=tmp_path)
    assert store.get_user(UserId("user-alice")) is None
    store.record_user(_record("user-alice", "alice", "Alice"))
    store.record_user(_record("user-bob", "bob"))
    alice = store.get_user(UserId("user-alice"))
    assert alice is not None and alice.desktop_id == "alice" and alice.display_name == "Alice"
    assert alice.email == "user-alice@example.com"

    later = _record("user-alice", "alice-2", "Alice")
    renamed = later.model_copy_update(to_update(later.field_ref().last_seen, TEST_NOW + timedelta(days=1)))
    store.record_user(renamed)
    replaced = store.get_user(UserId("user-alice"))
    assert replaced is not None and replaced.desktop_id == "alice-2"
    assert sorted(str(record.user_id) for record in store.list_users()) == ["user-alice", "user-bob"]
    assert json.loads((tmp_path / USERS_FILENAME).read_text())["version"] == 1


def test_an_unreadable_or_foreign_users_file_reads_as_empty(tmp_path: Path) -> None:
    store = UserStore(state_directory=tmp_path)
    (tmp_path / USERS_FILENAME).write_text(json.dumps({"version": 1, "users": {"user-x": {"desktop_id": 7}}}))
    assert store.list_users() == []
    (tmp_path / USERS_FILENAME).write_text(json.dumps({"version": 9, "users": {}}))
    assert store.list_users() == []
    (tmp_path / USERS_FILENAME).write_text(
        json.dumps({"version": 1, "users": {"not a user id!": _record("user-a", "a").model_dump(mode="json")}})
    )
    assert store.list_users() == []
