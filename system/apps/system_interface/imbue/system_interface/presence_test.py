import json
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest
from workspace_layout.identity import ANONYMOUS_OWNER
from workspace_layout.identity import RequestIdentity
from workspace_layout.primitives import OWNER_PARTICIPANT_KEY
from workspace_layout.primitives import ParticipantKey

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.presence import PRESENCE_CONNECTED_WINDOW
from imbue.system_interface.presence import PresenceStore
from imbue.system_interface.presence import PresenceSweep
from imbue.system_interface.presence import PresentUser
from imbue.system_interface.presence import present_user_wire_json
from imbue.system_interface.presence import present_users_wire_json
from imbue.system_interface.presence import utc_now
from imbue.system_interface.profiles import ProfileResolver
from imbue.system_interface.profiles import UserProfile
from imbue.system_interface.shell.errors import InvalidShellValueError

_T0 = datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc)
_BOB = RequestIdentity(owner=False, user_id="user-bob-4471", email="bob@example.com")
_OWNER = RequestIdentity(owner=True, user_id="user-owner-9c21", email="owner@example.com")


def _store(tmp_path: Path) -> PresenceStore:
    return PresenceStore(directory=tmp_path / "presence")


def test_the_first_heartbeat_writes_the_identity_snapshot_and_connects_the_user(tmp_path: Path) -> None:
    store = _store(tmp_path)

    outcome = store.heartbeat(_BOB, _T0)

    assert outcome.is_membership_changed is True
    (user,) = outcome.users
    assert user.user_id == "user-bob-4471"
    assert user.owner is False
    assert user.first_seen == user.last_seen == "2026-09-19T10:00:00.000000000Z"
    on_disk = json.loads((store.users_directory / "user-bob-4471.json").read_text())
    assert on_disk == {
        "user_id": "user-bob-4471",
        "email": "bob@example.com",
        "owner": False,
        "first_seen": "2026-09-19T10:00:00.000000000Z",
        "last_input_at": None,
    }


def test_a_later_heartbeat_touches_the_file_instead_of_rewriting_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.heartbeat(_BOB, _T0)
    path = store.users_directory / "user-bob-4471.json"
    path.write_text(path.read_text() + "\n")
    bytes_after_first = path.read_bytes()

    outcome = store.heartbeat(_BOB, _T0 + timedelta(seconds=30))

    assert outcome.is_membership_changed is False
    (user,) = outcome.users
    assert user.first_seen == "2026-09-19T10:00:00.000000000Z"
    assert user.last_seen == "2026-09-19T10:00:30.000000000Z"
    assert path.read_bytes() == bytes_after_first


def test_a_changed_email_or_owner_flag_rewrites_the_snapshot_and_keeps_first_seen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.heartbeat(_BOB, _T0)
    renamed = RequestIdentity(owner=False, user_id="user-bob-4471", email="robert@example.com")

    (user,) = store.heartbeat(renamed, _T0 + timedelta(seconds=30)).users

    assert user.email == "robert@example.com"
    assert user.first_seen == "2026-09-19T10:00:00.000000000Z"
    on_disk = json.loads((store.users_directory / "user-bob-4471.json").read_text())
    assert on_disk["email"] == "robert@example.com" and on_disk["first_seen"] == user.first_seen


def test_a_user_is_connected_while_the_mtime_is_within_the_window_and_the_file_is_kept_after(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.heartbeat(_BOB, _T0)

    assert [
        user.user_id for user in store.connected_users(_T0 + PRESENCE_CONNECTED_WINDOW - timedelta(seconds=1))
    ] == ["user-bob-4471"]
    assert store.connected_users(_T0 + PRESENCE_CONNECTED_WINDOW) == []
    # Gone is not forgotten: the file is the record of who was last here, and of when.
    assert (store.users_directory / "user-bob-4471.json").exists()
    (last_here,) = store.all_users()
    assert last_here.last_seen == "2026-09-19T10:00:00.000000000Z"


def test_a_heartbeat_reports_the_membership_change_an_expiry_made_since_the_last_report(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.heartbeat(_BOB, _T0)
    store.heartbeat(_OWNER, _T0 + timedelta(seconds=60))

    # Bob's file is past the window by the owner's next heartbeat, so the connected set shrank.
    outcome = store.heartbeat(_OWNER, _T0 + PRESENCE_CONNECTED_WINDOW + timedelta(seconds=1))

    assert outcome.is_membership_changed is True
    assert [user.user_id for user in outcome.users] == ["user-owner-9c21"]
    # And again nothing changed.
    assert (
        store.heartbeat(_OWNER, _T0 + PRESENCE_CONNECTED_WINDOW + timedelta(seconds=2)).is_membership_changed is False
    )


def test_the_sweep_reports_a_silent_departure_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.heartbeat(_BOB, _T0)

    unchanged = store.sweep(_T0 + timedelta(seconds=10))
    assert unchanged.is_membership_changed is False and [user.user_id for user in unchanged.users] == ["user-bob-4471"]

    departed = store.sweep(_T0 + PRESENCE_CONNECTED_WINDOW)
    assert departed.is_membership_changed is True and departed.users == ()

    assert store.sweep(_T0 + PRESENCE_CONNECTED_WINDOW + timedelta(seconds=10)).is_membership_changed is False


def test_the_sweep_announces_the_connected_set_only_when_it_changed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    announced: list[list[str]] = []

    def record(users: Sequence[PresentUser]) -> None:
        announced.append([user.user_id for user in users])

    sweep = PresenceSweep(store=store, announce=record, announce_roster=lambda: None)
    store.heartbeat(_BOB, _T0)

    assert sweep.sweep_once(_T0 + timedelta(seconds=10)) is False
    assert sweep.sweep_once(_T0 + PRESENCE_CONNECTED_WINDOW) is True
    assert sweep.sweep_once(_T0 + PRESENCE_CONNECTED_WINDOW + timedelta(seconds=10)) is False
    assert announced == [[]]


def test_a_started_sweep_announces_on_its_own_interval_and_ends_when_stopped(tmp_path: Path) -> None:
    # A store from an earlier process left a fresh file, which a new store's first sweep announces
    # (its own heartbeat path would have reported the change already).
    _store(tmp_path).heartbeat(_BOB, utc_now())
    announced: list[list[str]] = []

    def record(users: Sequence[PresentUser]) -> None:
        announced.append([user.user_id for user in users])

    sweep = PresenceSweep(store=_store(tmp_path), interval_seconds=0.01, announce=record, announce_roster=lambda: None)
    sweep.start()
    try:
        assert sweep.is_running is True
        wait_for(lambda: len(announced) >= 1, timeout=5.0, poll_interval=0.01, error_message="the sweep never ran")
    finally:
        sweep.stop()

    assert sweep.is_running is False
    assert announced == [["user-bob-4471"]]


def test_a_fresh_store_announces_files_left_by_an_earlier_process_when_they_are_still_fresh(tmp_path: Path) -> None:
    earlier = _store(tmp_path)
    earlier.heartbeat(_BOB, _T0)

    restarted = _store(tmp_path)
    outcome = restarted.sweep(_T0 + timedelta(seconds=5))

    assert outcome.is_membership_changed is True
    assert [user.user_id for user in outcome.users] == ["user-bob-4471"]


def test_the_owner_of_an_unshared_workspace_is_recorded_under_the_owner_key_and_kept_off_the_wire(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    (owner,) = store.heartbeat(ANONYMOUS_OWNER, _T0).users

    assert owner.user_id == "owner" and owner.owner is True and owner.is_anonymous_owner is True
    assert owner.participant == "owner"
    assert (store.users_directory / "owner.json").exists()
    assert present_users_wire_json([owner], ProfileResolver(cache_directory=tmp_path, share_env_path=tmp_path / "x"), _T0) == []
    assert store.presence_by_participant(_T0)[OWNER_PARTICIPANT_KEY].is_connected is True


def test_a_visiting_agent_cannot_be_recorded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    agent = RequestIdentity(owner=False, agent_id="va-0123", agent_name="Visitor", granter="user:8c0f")
    with pytest.raises(InvalidShellValueError):
        store.heartbeat(agent, _T0)
    assert store.connected_users(_T0) == []


def test_a_heartbeat_keeps_the_newest_input_time_and_a_late_one_never_moves_it_back(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.heartbeat(_BOB, _T0, last_input_at=_T0 - timedelta(seconds=5))
    store.heartbeat(_BOB, _T0 + timedelta(seconds=30), last_input_at=_T0 + timedelta(seconds=20))
    (user,) = store.heartbeat(_BOB, _T0 + timedelta(seconds=60), last_input_at=_T0 + timedelta(seconds=10)).users

    assert user.last_input_at == "2026-09-19T10:00:20.000000000Z"
    presence = store.presence_by_participant(_T0 + timedelta(seconds=60))[ParticipantKey("user:user-bob-4471")]
    assert presence.last_input_at == "2026-09-19T10:00:20.000000000Z"
    assert presence.is_connected is True and presence.email == "bob@example.com"


def test_an_unreadable_user_file_is_skipped_and_replaced_on_the_next_heartbeat(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.users_directory.mkdir(parents=True)
    (store.users_directory / "user-bob-4471.json").write_text("not json")
    (store.users_directory / "not a user id.json").write_text("{}")

    assert store.all_users() == []
    outcome = store.heartbeat(_BOB, _T0)

    assert [user.user_id for user in outcome.users] == ["user-bob-4471"]


def test_present_user_wire_json_carries_the_profile_beside_the_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    (user,) = store.heartbeat(_BOB, _T0).users
    profile = UserProfile(user_id="user-bob-4471", display_name="Bob", profile_picture_url="https://a/bob.png")

    assert present_user_wire_json(user, profile) == {
        "user_id": "user-bob-4471",
        "email": "bob@example.com",
        "display_name": "Bob",
        "profile_picture_url": "https://a/bob.png",
        "owner": False,
        "first_seen": "2026-09-19T10:00:00.000000000Z",
        "last_seen": "2026-09-19T10:00:00.000000000Z",
        "last_input_at": None,
    }
    without_profile = present_user_wire_json(user, None)
    assert without_profile["display_name"] is None and without_profile["profile_picture_url"] is None
    assert "session_count" not in without_profile
