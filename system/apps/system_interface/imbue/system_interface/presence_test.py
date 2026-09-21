import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.system_interface.presence import ANONYMOUS_OWNER
from imbue.system_interface.presence import PRESENCE_SESSION_TTL
from imbue.system_interface.presence import PresenceSessionId
from imbue.system_interface.presence import PresenceStore
from imbue.system_interface.presence import RequestIdentity
from imbue.system_interface.presence import parse_identity_header
from imbue.system_interface.presence import parse_presence_timestamp
from imbue.system_interface.presence import present_user_wire_json
from imbue.system_interface.shell.errors import InvalidShellValueError

_T0 = datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc)
_BOB = RequestIdentity(
    owner=False,
    user_id="user-bob-4471",
    email="bob@example.com",
    display_name="Bob",
    avatar_url="https://accounts.example.com/users/user-bob-4471/avatar/9a7b",
)
_OWNER = RequestIdentity(owner=True, user_id="user-owner-9c21", email="owner@example.com")
_TAB_ONE = PresenceSessionId("tab-0001-aaaa")
_TAB_TWO = PresenceSessionId("tab-0002-bbbb")


def _store(tmp_path: Path) -> PresenceStore:
    return PresenceStore(directory=tmp_path / "presence")


def test_parse_identity_header_reads_the_record_and_ignores_unknown_fields() -> None:
    header = json.dumps(
        {
            "owner": False,
            "user_id": "user-bob-4471",
            "email": "bob@example.com",
            "display_name": "Bob",
            "avatar_url": "https://a/b",
            "a_field_from_a_newer_proxy": 1,
        }
    )
    assert parse_identity_header(header) == RequestIdentity(
        owner=False, user_id="user-bob-4471", email="bob@example.com", display_name="Bob", avatar_url="https://a/b"
    )


def test_parse_identity_header_treats_absence_and_garbage_as_the_anonymous_owner() -> None:
    assert parse_identity_header(None) == ANONYMOUS_OWNER
    assert parse_identity_header("   ") == ANONYMOUS_OWNER
    assert parse_identity_header("not json") == ANONYMOUS_OWNER
    assert parse_identity_header('{"user_id": "x"}') == ANONYMOUS_OWNER
    assert parse_identity_header('{"owner": true}') == ANONYMOUS_OWNER
    assert ANONYMOUS_OWNER.user_id is None


def test_parse_presence_timestamp_reads_the_nanosecond_form() -> None:
    assert parse_presence_timestamp("2026-09-19T10:00:00.123456789Z") == datetime(
        2026, 9, 19, 10, 0, 0, 123456, tzinfo=timezone.utc
    )
    assert parse_presence_timestamp("2026-09-19T10:00:00") is None
    assert parse_presence_timestamp("garbage-Z") is None


@pytest.mark.parametrize("session_id", ["", "short", "has space 0001", "../etc/passwd"])
def test_session_ids_are_held_to_a_filename_safe_alphabet(session_id: str) -> None:
    with pytest.raises(InvalidShellValueError):
        PresenceSessionId(session_id)


def test_heartbeat_creates_the_user_file_and_a_joined_event(tmp_path: Path) -> None:
    store = _store(tmp_path)

    outcome = store.heartbeat(_BOB, _TAB_ONE, _T0)

    assert outcome.is_membership_changed is True
    (user,) = outcome.users
    assert user.user_id == "user-bob-4471"
    assert user.display_name == "Bob"
    assert user.avatar_url == _BOB.avatar_url
    assert user.owner is False
    assert set(user.sessions) == {str(_TAB_ONE)}
    assert user.first_seen == user.last_seen
    on_disk = json.loads((store.users_directory / "user-bob-4471.json").read_text())
    assert on_disk["email"] == "bob@example.com"
    assert on_disk["sessions"][str(_TAB_ONE)] == user.last_seen
    (event,) = store.read_events()
    assert event["type"] == "user_joined"
    assert event["source"] == "presence"
    assert event["user_id"] == "user-bob-4471"
    assert event["event_id"].startswith("evt-")
    assert event["timestamp"] == user.first_seen


def test_a_second_heartbeat_updates_the_snapshot_without_a_new_join(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.heartbeat(_BOB, _TAB_ONE, _T0)
    renamed = _BOB.model_copy_update(to_update(_BOB.field_ref().display_name, "Robert"))

    outcome = store.heartbeat(renamed, _TAB_ONE, _T0 + timedelta(seconds=30))

    assert outcome.is_membership_changed is False
    (user,) = outcome.users
    assert user.display_name == "Robert"
    assert user.first_seen != user.last_seen
    assert [event["type"] for event in store.read_events()] == ["user_joined"]


def test_two_tabs_are_one_user_who_leaves_with_the_last_one(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.heartbeat(_BOB, _TAB_ONE, _T0)
    two_tabs = store.heartbeat(_BOB, _TAB_TWO, _T0 + timedelta(seconds=1))
    assert two_tabs.is_membership_changed is False
    assert set(two_tabs.users[0].sessions) == {str(_TAB_ONE), str(_TAB_TWO)}

    first_leave = store.leave(_BOB, _TAB_ONE, _T0 + timedelta(seconds=2))
    assert first_leave.is_membership_changed is False
    assert set(first_leave.users[0].sessions) == {str(_TAB_TWO)}

    last_leave = store.leave(_BOB, _TAB_TWO, _T0 + timedelta(seconds=3))
    assert last_leave.is_membership_changed is True
    assert last_leave.users == ()
    assert not (store.users_directory / "user-bob-4471.json").exists()
    assert [event["type"] for event in store.read_events()] == ["user_joined", "user_left"]


def test_sessions_expire_after_the_ttl_and_the_user_leaves(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.heartbeat(_BOB, _TAB_ONE, _T0)
    store.heartbeat(_OWNER, _TAB_TWO, _T0 + timedelta(seconds=60))

    # Bob's only session is past the TTL by the time the owner heartbeats again.
    outcome = store.heartbeat(_OWNER, _TAB_TWO, _T0 + PRESENCE_SESSION_TTL + timedelta(seconds=1))

    assert outcome.is_membership_changed is True
    assert [user.user_id for user in outcome.users] == ["user-owner-9c21"]
    assert not (store.users_directory / "user-bob-4471.json").exists()
    left_events = [event for event in store.read_events() if event["type"] == "user_left"]
    assert [event["user_id"] for event in left_events] == ["user-bob-4471"]


def test_connected_users_reads_through_the_ttl_without_writing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.heartbeat(_BOB, _TAB_ONE, _T0)

    later = _T0 + PRESENCE_SESSION_TTL
    assert store.connected_users(later) == []
    # A read prunes nothing on disk: the next heartbeat or leave does.
    assert (store.users_directory / "user-bob-4471.json").exists()
    assert store.read_events()[-1]["type"] == "user_joined"


def test_leaving_an_unknown_session_changes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.heartbeat(_BOB, _TAB_ONE, _T0)

    outcome = store.leave(_BOB, _TAB_TWO, _T0 + timedelta(seconds=1))

    assert outcome.is_membership_changed is False
    assert set(outcome.users[0].sessions) == {str(_TAB_ONE)}


def test_an_identity_without_a_user_id_cannot_be_recorded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(InvalidShellValueError):
        store.heartbeat(ANONYMOUS_OWNER, _TAB_ONE, _T0)
    assert store.connected_users(_T0) == []


def test_an_unreadable_user_file_is_skipped_and_replaced_on_the_next_heartbeat(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.users_directory.mkdir(parents=True)
    (store.users_directory / "user-bob-4471.json").write_text("not json")
    (store.users_directory / "not a user id.json").write_text("{}")

    assert store.connected_users(_T0) == []
    outcome = store.heartbeat(_BOB, _TAB_ONE, _T0)

    assert [user.user_id for user in outcome.users] == ["user-bob-4471"]


def test_present_user_wire_json_counts_sessions_instead_of_listing_them(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.heartbeat(_BOB, _TAB_ONE, _T0)
    (user,) = store.heartbeat(_BOB, _TAB_TWO, _T0).users

    wire = present_user_wire_json(user)

    assert wire["session_count"] == 2
    assert "sessions" not in wire
    assert wire["user_id"] == "user-bob-4471"
    assert wire["display_name"] == "Bob"
    assert wire["owner"] is False
