import json
from datetime import timedelta
from pathlib import Path

import pytest
from app_manifest.manifest import EntryMode
from app_manifest.manifest import PinStyle
from app_manifest.primitives import AppName
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import UserId
from workspace_layout.primitives import WindowId
from workspace_layout.records import EntryPresentation
from workspace_layout.records import FloatingPosition

from imbue.system_interface.shell.clients import CLIENTS_FILENAME
from imbue.system_interface.shell.clients import CLIENT_RETENTION
from imbue.system_interface.shell.clients import ClientStore
from imbue.system_interface.shell.clients import SHOWN_HISTORY_LIMIT
from imbue.system_interface.shell.clients import client_view
from imbue.system_interface.shell.errors import ClientNotFoundError
from imbue.system_interface.shell.testing import TEST_NOW
from imbue.system_interface.shell.testing import client_report


def test_reports_are_recorded_and_listed_newest_first(tmp_path: Path) -> None:
    store = ClientStore(state_directory=tmp_path)
    assert (
        store.record_report(client_report("c1", "home"), TEST_NOW, is_redirected=False).is_active_desktop_changed
        is True
    )
    store.record_report(client_report("c2", "research"), TEST_NOW + timedelta(minutes=1), is_redirected=False)
    outcome = store.record_report(
        client_report("c1", "research"), TEST_NOW + timedelta(minutes=2), is_redirected=False
    )
    recorded = outcome.record
    assert outcome.is_active_desktop_changed is True
    unmoved = store.record_report(
        client_report("c1", "research"), TEST_NOW + timedelta(minutes=3), is_redirected=False
    )
    assert unmoved.is_active_desktop_changed is False
    # Each move bumps the desktop revision; a report naming the desktop already stored does not.
    assert (recorded.desktop_revision, unmoved.record.desktop_revision) == (2, 2)
    assert [str(client.id) for client in store.list_clients()] == ["c1", "c2"]
    assert recorded.active_desktop == "research"
    assert store.get_client("missing") is None
    assert client_view(recorded, True).model_dump(mode="json") == {
        "id": "c1",
        "active_desktop": "research",
        "last_seen": "2026-09-04T00:02:00Z",
        "is_connected": True,
        "user_id": None,
        "entries": {},
        "shown_history": [],
        "desktop_revision": 2,
    }
    assert json.loads((tmp_path / CLIENTS_FILENAME).read_text())["version"] == 2


def test_set_active_desktop_moves_a_recorded_client_and_refuses_an_unknown_one(tmp_path: Path) -> None:
    store = ClientStore(state_directory=tmp_path)
    store.record_report(client_report("c1", "home"), TEST_NOW, is_redirected=False)
    moved = store.set_active_desktop(ClientId("c1"), DesktopId("research"), TEST_NOW + timedelta(minutes=1))
    assert moved.is_active_desktop_changed is True and moved.record.active_desktop == "research"
    unmoved = store.set_active_desktop(ClientId("c1"), DesktopId("research"), TEST_NOW)
    assert unmoved.is_active_desktop_changed is False
    assert (moved.record.desktop_revision, unmoved.record.desktop_revision) == (2, 2)
    with pytest.raises(ClientNotFoundError):
        store.set_active_desktop(ClientId("nobody"), DesktopId("research"), TEST_NOW)


def test_a_report_made_before_a_move_its_page_had_not_heard_of_is_superseded(tmp_path: Path) -> None:
    store = ClientStore(state_directory=tmp_path)
    page = "page-00000000000000aa"
    store.record_report(client_report("c1", "home", 0, page), TEST_NOW, is_redirected=False)
    # An op moves the client while the page's next report, made at revision 1, is on its way.
    store.set_active_desktop(ClientId("c1"), DesktopId("research"), TEST_NOW)
    stale = store.record_report(
        client_report("c1", "home", 1, page), TEST_NOW + timedelta(minutes=1), is_redirected=False
    )
    assert (stale.is_superseded, stale.is_active_desktop_changed) == (True, False)
    recorded = store.get_client("c1")
    assert recorded is not None
    assert (recorded.active_desktop, recorded.desktop_revision, recorded.last_seen) == ("research", 2, TEST_NOW)
    # Once the page has heard the op's move, its report moves the client again.
    heard = store.record_report(client_report("c1", "home", 2, page), TEST_NOW, is_redirected=False)
    assert (heard.is_superseded, heard.is_active_desktop_changed, heard.record.desktop_revision) == (False, True, 3)


def test_a_page_reports_past_its_own_moves_but_not_past_another_pages(tmp_path: Path) -> None:
    store = ClientStore(state_directory=tmp_path)
    page, other_page = "page-00000000000000aa", "page-00000000000000bb"
    store.record_report(client_report("c1", "home", 0, page), TEST_NOW, is_redirected=False)
    # Rapid switches: every report is made at revision 1, before the page has heard its own moves.
    for desktop, revision in (("research", 2), ("home", 3), ("research", 4)):
        outcome = store.record_report(client_report("c1", desktop, 1, page), TEST_NOW, is_redirected=False)
        assert (outcome.is_superseded, outcome.record.desktop_revision) == (False, revision)
    # A second window of the client that has not heard them is superseded, and so is the first once it has moved.
    assert store.record_report(client_report("c1", "home", 1, other_page), TEST_NOW, is_redirected=False).is_superseded
    store.record_report(client_report("c1", "home", 4, other_page), TEST_NOW, is_redirected=False)
    assert store.record_report(client_report("c1", "research", 1, page), TEST_NOW, is_redirected=False).is_superseded


def test_an_entry_presentation_is_kept_on_the_client_across_its_reports_and_refused_for_an_unknown_client(
    tmp_path: Path,
) -> None:
    store = ClientStore(state_directory=tmp_path)
    store.record_report(client_report("c1", "home"), TEST_NOW, is_redirected=False)
    floating = EntryPresentation(
        mode=EntryMode.FLOATING, style=PinStyle.AVATAR, position=FloatingPosition(x=0.9, y=0.85)
    )
    record = store.set_entry_presentation(ClientId("c1"), AppName("chat"), floating, TEST_NOW + timedelta(minutes=1))
    assert record.entries == {"chat": floating}
    assert record.last_seen == TEST_NOW + timedelta(minutes=1)
    # A later report and a desktop move keep the presentation; a second app's entry sits beside it.
    store.record_report(client_report("c1", "research"), TEST_NOW + timedelta(minutes=2), is_redirected=False)
    store.set_active_desktop(ClientId("c1"), DesktopId("home"), TEST_NOW + timedelta(minutes=3))
    bar = EntryPresentation(mode=EntryMode.BAR, style=PinStyle.PLAIN, position=None)
    both = store.set_entry_presentation(ClientId("c1"), AppName("notes"), bar, TEST_NOW + timedelta(minutes=4))
    assert both.entries == {"chat": floating, "notes": bar}
    assert client_view(both, False).model_dump(mode="json")["entries"] == {
        "chat": {"mode": "floating", "style": "avatar", "position": {"x": 0.9, "y": 0.85}},
        "notes": {"mode": "bar", "style": "plain", "position": None},
    }
    stored = json.loads((tmp_path / CLIENTS_FILENAME).read_text())["clients"]["c1"]["entries"]
    assert set(stored) == {"chat", "notes"}
    with pytest.raises(ClientNotFoundError):
        store.set_entry_presentation(ClientId("nobody"), AppName("chat"), bar, TEST_NOW)


def test_clients_unseen_for_the_retention_period_are_pruned(tmp_path: Path) -> None:
    store = ClientStore(state_directory=tmp_path)
    store.record_report(
        client_report("old", "home"), TEST_NOW - CLIENT_RETENTION - timedelta(days=1), is_redirected=False
    )
    store.record_report(client_report("fresh", "home"), TEST_NOW - timedelta(days=1), is_redirected=False)
    assert store.prune_unseen(TEST_NOW) == [ClientId("old")]
    assert [str(client.id) for client in store.list_clients()] == ["fresh"]
    assert store.prune_unseen(TEST_NOW) == []


def test_a_version_one_file_is_read_with_the_view_as_the_desktop_and_rewritten_at_version_two(tmp_path: Path) -> None:
    """The tabbed shell's file carried a device kind and a view per client; a client that reported a desktop keeps
    it, one that never did lands on the desktop of its view's id (or the first desktop, when none has it)."""
    legacy = {
        "version": 1,
        "clients": {
            "viewer": {"device_kind": "mobile", "active_view": "alpha", "last_seen": "2026-09-01T00:00:00+00:00"},
            "desktopper": {
                "device_kind": "desktop",
                "active_view": "everything",
                "active_desktop": "home",
                "last_seen": "2026-09-02T00:00:00+00:00",
            },
        },
    }
    (tmp_path / CLIENTS_FILENAME).write_text(json.dumps(legacy))
    store = ClientStore(state_directory=tmp_path)

    by_id = {str(client.id): client for client in store.list_clients()}
    assert by_id["viewer"].active_desktop == "alpha"
    assert by_id["desktopper"].active_desktop == "home"

    store.record_report(client_report("c3", "home"), TEST_NOW, is_redirected=False)
    written = json.loads((tmp_path / CLIENTS_FILENAME).read_text())
    assert written["version"] == 2
    assert set(written["clients"]) == {"viewer", "desktopper", "c3"}
    assert set(written["clients"]["viewer"]) == {
        "active_desktop",
        "last_seen",
        "user_id",
        "entries",
        "shown_history",
        "desktop_revision",
        "desktop_moved_by",
    }


def test_a_file_of_an_unknown_version_or_shape_is_treated_as_empty(tmp_path: Path) -> None:
    (tmp_path / CLIENTS_FILENAME).write_text(json.dumps({"version": 7, "clients": {}}))
    assert ClientStore(state_directory=tmp_path).list_clients() == []
    (tmp_path / CLIENTS_FILENAME).write_text(json.dumps({"version": 2, "clients": "nope"}))
    assert ClientStore(state_directory=tmp_path).list_clients() == []


def test_an_arrival_records_the_user_and_the_landing_desktop_and_a_report_keeps_the_user(tmp_path: Path) -> None:
    store = ClientStore(state_directory=tmp_path)
    arrived = store.record_arrival(ClientId("c1"), UserId("user-alice"), DesktopId("alice"), TEST_NOW)
    assert arrived.is_active_desktop_changed is True
    assert arrived.record.user_id == "user-alice" and arrived.record.active_desktop == "alice"
    # The client_state report, made by a page that read the arrival's record, carries no user, so the recorded one
    # stays.
    reported = store.record_report(
        client_report("c1", "home", arrived.record.desktop_revision),
        TEST_NOW + timedelta(minutes=1),
        is_redirected=False,
    ).record
    assert reported.user_id == "user-alice" and reported.active_desktop == "home"
    # Arriving again on the desktop the client is on changes nothing about the desktop.
    assert (
        store.record_arrival(
            ClientId("c1"), UserId("user-alice"), DesktopId("home"), TEST_NOW
        ).is_active_desktop_changed
        is False
    )
    assert client_view(reported, False).model_dump(mode="json")["user_id"] == "user-alice"


def _window_id(index: int) -> str:
    return f"win-{index:016x}"


def test_the_shown_history_holds_the_newest_distinct_entries_most_recent_last(tmp_path: Path) -> None:
    store = ClientStore(state_directory=tmp_path)
    store.record_report(client_report("c1", "home"), TEST_NOW, is_redirected=False)
    for index in range(SHOWN_HISTORY_LIMIT + 5):
        store.record_shown(ClientId("c1"), WindowId(_window_id(index)), TEST_NOW)
    store.record_shown(ClientId("c1"), None, TEST_NOW)
    # Showing a window again moves it to the end rather than listing it twice.
    again = store.record_shown(ClientId("c1"), WindowId(_window_id(10)), TEST_NOW + timedelta(minutes=1))

    expected = (
        *(_window_id(index) for index in range(6, SHOWN_HISTORY_LIMIT + 5) if index != 10),
        "home",
        _window_id(10),
    )
    assert again.shown_history == expected
    assert len(again.shown_history) == SHOWN_HISTORY_LIMIT
    assert again.last_seen == TEST_NOW + timedelta(minutes=1)
    # A later report and arrival keep it, and it is what the store reads back.
    store.record_report(client_report("c1", "research"), TEST_NOW + timedelta(minutes=2), is_redirected=False)
    store.record_arrival(ClientId("c1"), None, DesktopId("home"), TEST_NOW + timedelta(minutes=3))
    reread = ClientStore(state_directory=tmp_path).get_client("c1")
    assert reread is not None and reread.shown_history == expected
    assert client_view(reread, False).model_dump(mode="json")["shown_history"] == list(expected)
    with pytest.raises(ClientNotFoundError):
        store.record_shown(ClientId("nobody"), None, TEST_NOW)


def test_dropping_closed_windows_prunes_every_clients_history_and_writes_nothing_otherwise(tmp_path: Path) -> None:
    store = ClientStore(state_directory=tmp_path)
    for client_id in ("c1", "c2"):
        store.record_report(client_report(client_id, "home"), TEST_NOW, is_redirected=False)
    for shown in (WindowId(_window_id(1)), None, WindowId(_window_id(2))):
        store.record_shown(ClientId("c1"), shown, TEST_NOW)
    store.record_shown(ClientId("c2"), WindowId(_window_id(2)), TEST_NOW)

    store.drop_windows([WindowId(_window_id(2))])

    by_id = {str(client.id): client.shown_history for client in store.list_clients()}
    assert by_id == {"c1": (_window_id(1), "home"), "c2": ()}
    written = (tmp_path / CLIENTS_FILENAME).stat().st_ino
    store.drop_windows([WindowId(_window_id(9))])
    assert (tmp_path / CLIENTS_FILENAME).stat().st_ino == written


def test_a_version_two_file_written_before_the_shown_history_reads_it_as_empty(tmp_path: Path) -> None:
    earlier = {
        "version": 2,
        "clients": {
            "c1": {"active_desktop": "home", "last_seen": "2026-09-01T00:00:00+00:00", "user_id": None, "entries": {}}
        },
    }
    (tmp_path / CLIENTS_FILENAME).write_text(json.dumps(earlier))
    store = ClientStore(state_directory=tmp_path)

    (client,) = store.list_clients()
    assert client.shown_history == ()
    assert store.record_shown(ClientId("c1"), WindowId(_window_id(1)), TEST_NOW).shown_history == (_window_id(1),)
    assert json.loads((tmp_path / CLIENTS_FILENAME).read_text())["version"] == 2
