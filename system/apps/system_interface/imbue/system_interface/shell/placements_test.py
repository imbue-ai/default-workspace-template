from datetime import timedelta
from pathlib import Path

import pytest

from imbue.system_interface.shell.data_types import WindowPlacement
from imbue.system_interface.shell.desktop_document import cascade_frame
from imbue.system_interface.shell.desktop_document import with_window_placed_on_open
from imbue.system_interface.shell.errors import StalePlacementsSaveError
from imbue.system_interface.shell.placements import PlacementStore
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowState
from imbue.system_interface.shell.testing import TEST_NOW

_WIN_1 = WindowId("win-0000000000000001")
_WIN_2 = WindowId("win-0000000000000002")
_LIVE = frozenset({_WIN_1, _WIN_2})


def _placement(window_id: WindowId) -> WindowPlacement:
    return WindowPlacement(window_id=window_id, frame=cascade_frame(0), state=WindowState.NORMAL, is_minimized=False)


def test_a_layout_reads_empty_until_written_and_drops_placements_of_closed_windows(tmp_path: Path) -> None:
    store = PlacementStore(state_directory=tmp_path)
    empty = store.read_layout("home", "c1", _LIVE)
    assert empty.placements == () and empty.updated_at is None
    saved = store.save_browser_layout(
        "home", "c1", [_placement(_WIN_1), _placement(WindowId("win-00000000000000ff"))], None, _LIVE, TEST_NOW
    )
    assert saved is not None and [placement.window_id for placement in saved.placements] == [_WIN_1]
    assert saved.updated_at == TEST_NOW
    assert store.read_layout("home", "c1", frozenset({_WIN_2})).placements == ()
    assert (tmp_path / "placements" / "home" / "c1.json").is_file()


def test_a_browser_save_is_refused_when_stale_and_skipped_when_unchanged(tmp_path: Path) -> None:
    store = PlacementStore(state_directory=tmp_path)
    first = store.save_browser_layout("home", "c1", [_placement(_WIN_1)], None, _LIVE, TEST_NOW)
    assert first is not None
    with pytest.raises(StalePlacementsSaveError):
        store.save_browser_layout("home", "c1", [], None, _LIVE, TEST_NOW + timedelta(seconds=1))
    with pytest.raises(StalePlacementsSaveError):
        store.save_browser_layout(
            "home", "c1", [], TEST_NOW - timedelta(seconds=1), _LIVE, TEST_NOW + timedelta(seconds=1)
        )
    assert (
        store.save_browser_layout(
            "home", "c1", [_placement(_WIN_1)], first.updated_at, _LIVE, TEST_NOW + timedelta(seconds=1)
        )
        is None
    )
    second = store.save_browser_layout("home", "c1", [], first.updated_at, _LIVE, TEST_NOW + timedelta(seconds=2))
    assert second is not None and second.placements == ()


def test_the_shells_own_edit_writes_only_a_change_and_a_close_drops_the_window_everywhere(tmp_path: Path) -> None:
    store = PlacementStore(state_directory=tmp_path)
    opened = store.edit_layout(
        "home", "c1", _LIVE, lambda layout: with_window_placed_on_open(layout, _WIN_1), TEST_NOW
    )
    assert opened.is_written is True and [placement.window_id for placement in opened.layout.placements] == [_WIN_1]
    unchanged = store.edit_layout("home", "c1", _LIVE, lambda layout: layout, TEST_NOW + timedelta(seconds=1))
    assert unchanged.is_written is False and unchanged.layout.updated_at == TEST_NOW
    store.edit_layout("home", "c2", _LIVE, lambda layout: with_window_placed_on_open(layout, _WIN_2), TEST_NOW)
    store.edit_layout("home", "c2", _LIVE, lambda layout: with_window_placed_on_open(layout, _WIN_1), TEST_NOW)

    rewritten = store.drop_window_everywhere("home", _WIN_1, TEST_NOW + timedelta(seconds=5))
    assert sorted(str(stored.client_id) for stored in rewritten) == ["c1", "c2"]
    assert store.read_layout("home", "c1", _LIVE).placements == ()
    assert [placement.window_id for placement in store.read_layout("home", "c2", _LIVE).placements] == [_WIN_2]
    assert store.drop_window_everywhere("home", _WIN_1, TEST_NOW) == []


def test_layouts_are_deleted_per_desktop_and_per_client(tmp_path: Path) -> None:
    store = PlacementStore(state_directory=tmp_path)
    for desktop_id in ("home", "alpha"):
        for client_id in ("c1", "c2"):
            store.edit_layout(
                desktop_id, client_id, _LIVE, lambda layout: with_window_placed_on_open(layout, _WIN_1), TEST_NOW
            )
    assert [str(stored.client_id) for stored in store.layouts_of_desktop("home")] == ["c1", "c2"]
    store.delete_desktop_layouts("home")
    assert store.layouts_of_desktop("home") == []
    assert not (tmp_path / "placements" / "home").exists()
    assert store.delete_client_layouts(ClientId("c1")) == 1
    assert [str(stored.client_id) for stored in store.layouts_of_desktop("alpha")] == ["c2"]
    assert store.delete_client_layouts(ClientId("c1")) == 0
