import json
from pathlib import Path

import pytest
from app_manifest.manifest import ShortcutMode
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId

from imbue.imbue_common.model_update import to_update
from imbue.system_interface.shell.data_types import ClientRecord
from imbue.system_interface.shell.data_types import DesktopShortcut
from imbue.system_interface.shell.data_types import GridCell
from imbue.system_interface.shell.data_types import ShortcutTarget
from imbue.system_interface.shell.data_types import Wallpaper
from imbue.system_interface.shell.desktops import DESKTOP_GLYPH_COLORS
from imbue.system_interface.shell.desktops import DesktopStore
from imbue.system_interface.shell.desktops import FALLBACK_USER_DESKTOP_NAME
from imbue.system_interface.shell.desktops import default_desktop
from imbue.system_interface.shell.desktops import desktop_name_for_user
from imbue.system_interface.shell.desktops import find_desktop_by_name_or_id
from imbue.system_interface.shell.desktops import next_glyph_index
from imbue.system_interface.shell.desktops import resolve_active_desktop
from imbue.system_interface.shell.desktops import slugify_desktop_name
from imbue.system_interface.shell.errors import DesktopConflictError
from imbue.system_interface.shell.errors import DesktopNotFoundError
from imbue.system_interface.shell.errors import DesktopValueError
from imbue.system_interface.shell.errors import LastDesktopError
from imbue.system_interface.shell.errors import WindowNotFoundError
from imbue.system_interface.shell.identity import RequestIdentity
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import GLYPH_COUNT
from imbue.system_interface.shell.primitives import WallpaperKind
from imbue.system_interface.shell.primitives import WallpaperName
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.shell.testing import TEST_NOW
from imbue.system_interface.shell.testing import window_record

_SEED = (
    DesktopShortcut(
        target=ShortcutTarget(app=AppName("chat"), launch=LaunchPathId("new")),
        mode=ShortcutMode.NEW,
        cell=GridCell(column=0, row=0),
    ),
)
_WIN_1 = WindowId("win-0000000000000001")


def test_the_default_desktop_is_created_once_on_the_first_read_that_may_seed(tmp_path: Path) -> None:
    store = DesktopStore(state_directory=tmp_path)
    assert store.list_desktops() == []
    assert not (tmp_path / "desktops.json").exists()
    seeded = store.ensure_default(lambda: _SEED)
    assert [desktop.name for desktop in seeded] == ["Home"]
    assert seeded[0].id == "home" and seeded[0].shortcuts == _SEED and seeded[0].wallpaper is None
    # A later read with another seed keeps what was written.
    assert store.ensure_default(lambda: ()) == seeded
    assert json.loads((tmp_path / "desktops.json").read_text())["version"] == 1


def test_a_file_of_another_version_or_shape_is_treated_as_absent(tmp_path: Path) -> None:
    store = DesktopStore(state_directory=tmp_path)
    (tmp_path / "desktops.json").write_text(json.dumps({"version": 7, "desktops": []}))
    assert store.list_desktops() == []
    (tmp_path / "desktops.json").write_text(json.dumps({"version": 1, "desktops": [{"id": "x"}]}))
    assert store.list_desktops() == []
    assert [desktop.id for desktop in store.ensure_default(lambda: ())] == ["home"]


def test_desktops_are_created_settled_and_deleted_with_the_last_one_refused(tmp_path: Path) -> None:
    store = DesktopStore(state_directory=tmp_path)
    home = store.ensure_default(lambda: ())[0]
    research = store.create_desktop("Research!", "#12B5A5", 4, _SEED)
    assert research.id == "research" and research.shortcuts == _SEED
    with pytest.raises(DesktopConflictError):
        store.create_desktop("research", "#12B5A5", 4, ())
    with pytest.raises(DesktopValueError):
        store.create_desktop("Bad", "red", 4, ())
    with pytest.raises(DesktopValueError):
        store.create_desktop("Bad", "#12B5A5", 12, ())
    with pytest.raises(DesktopValueError):
        slugify_desktop_name("!!!")
    settled = store.update_settings("research", "Research 2", "#222222", 2)
    assert (settled.id, settled.name, settled.color, settled.glyph) == ("research", "Research 2", "#222222", 2)
    papered = store.set_wallpaper("research", Wallpaper(kind=WallpaperKind.BUNDLED, name=WallpaperName("dunes")))
    assert papered.wallpaper is not None and papered.wallpaper.name == "dunes"
    assert store.set_wallpaper("research", None).wallpaper is None
    assert find_desktop_by_name_or_id(store.list_desktops(), "research 2") is not None
    assert find_desktop_by_name_or_id(store.list_desktops(), "nowhere") is None

    outcome = store.delete_desktop("home")
    assert outcome.deleted.id == home.id and outcome.fallback_desktop_id == "research"
    with pytest.raises(LastDesktopError):
        store.delete_desktop("research")
    with pytest.raises(DesktopNotFoundError):
        store.delete_desktop("home")
    assert [desktop.id for desktop in store.list_desktops()] == ["research"]


def test_windows_are_opened_located_and_closed_idempotently(tmp_path: Path) -> None:
    store = DesktopStore(state_directory=tmp_path)
    store.ensure_default(lambda: ())
    opened = store.open_window("home", window_record(_WIN_1, "chat", "/new?message=hi", is_settling=True))
    assert [window.id for window in opened.windows] == [_WIN_1]
    located = store.set_window_location("home", _WIN_1, WindowPath("/?chat=agent-1"), WindowTitle("Plan"))
    assert located.is_written is True and located.desktop.windows[0].is_settling is False
    again = store.set_window_location("home", _WIN_1, WindowPath("/?chat=agent-1"), WindowTitle("Plan"))
    assert again.is_written is False
    with pytest.raises(WindowNotFoundError):
        store.set_window_location("home", WindowId("win-00000000000000ff"), WindowPath("/"), WindowTitle(""))
    closed = store.close_window("home", _WIN_1)
    assert closed.is_written is True and closed.desktop.windows == ()
    assert store.close_window("home", _WIN_1).is_written is False
    with pytest.raises(DesktopNotFoundError):
        store.close_window("nowhere", _WIN_1)


def test_shortcuts_are_set_moved_and_removed(tmp_path: Path) -> None:
    store = DesktopStore(state_directory=tmp_path)
    store.ensure_default(lambda: _SEED)
    files = DesktopShortcut(
        target=ShortcutTarget(app=AppName("files"), launch=LaunchPathId("open")),
        mode=ShortcutMode.FOCUS,
        cell=GridCell(column=0, row=1),
    )
    assert [str(shortcut.target.app) for shortcut in store.set_shortcut("home", files).shortcuts] == ["chat", "files"]
    moved = store.move_shortcut("home", AppName("files"), LaunchPathId("open"), GridCell(column=0, row=0))
    assert {str(shortcut.target.app): shortcut.cell for shortcut in moved.shortcuts} == {
        "files": GridCell(column=0, row=0),
        "chat": GridCell(column=0, row=1),
    }
    removed = store.remove_shortcut("home", AppName("chat"), LaunchPathId("new"))
    assert [str(shortcut.target.app) for shortcut in removed.shortcuts] == ["files"]


def test_a_clients_active_desktop_falls_back_from_its_desktop_to_the_first(tmp_path: Path) -> None:
    store = DesktopStore(state_directory=tmp_path)
    home, alpha = store.ensure_default(lambda: ())[0], store.create_desktop("Alpha", "#111111", 1, ())
    desktops = [home, alpha]
    assert resolve_active_desktop(None, desktops) == home.id
    assert resolve_active_desktop(None, []) is None
    on_alpha = ClientRecord(id=ClientId("c1"), active_desktop=DesktopId("alpha"), last_seen=TEST_NOW)
    assert resolve_active_desktop(on_alpha, desktops) == alpha.id
    stale = ClientRecord(id=ClientId("c1"), active_desktop=DesktopId("gone"), last_seen=TEST_NOW)
    assert resolve_active_desktop(stale, desktops) == home.id
    unplaced = ClientRecord(id=ClientId("c1"), last_seen=TEST_NOW)
    assert resolve_active_desktop(unplaced, desktops) == home.id


def test_a_desktops_file_carrying_the_retired_sharing_key_still_reads(tmp_path: Path) -> None:
    store = DesktopStore(state_directory=tmp_path)
    home = store.ensure_default(lambda: ())[0]
    raw = json.loads((tmp_path / "desktops.json").read_text())
    raw["desktops"][0]["sharing"] = "personal"
    (tmp_path / "desktops.json").write_text(json.dumps(raw))
    assert store.list_desktops() == [home]
    store.update_settings("home", "Home", "#2f6b4f", 1)
    assert "sharing" not in json.loads((tmp_path / "desktops.json").read_text())["desktops"][0]


def test_a_users_desktop_is_named_after_them_and_made_unique() -> None:
    home = default_desktop(())
    alice = RequestIdentity(owner=False, user_id="user-alice", email="alice@example.com", display_name="Alice")
    assert desktop_name_for_user(alice, [home]) == "Alice"
    nameless = RequestIdentity(owner=False, user_id="user-bob", email="bob.smith@example.com")
    assert desktop_name_for_user(nameless, [home]) == "bob.smith"
    unusable = RequestIdentity(owner=False, user_id="user-x", email="!!!@example.com", display_name="   ")
    assert desktop_name_for_user(unusable, [home]) == FALLBACK_USER_DESKTOP_NAME
    taken = [
        home,
        home.model_copy_update(
            to_update(home.field_ref().id, DesktopId("alice")), to_update(home.field_ref().name, "alice")
        ),
    ]
    assert desktop_name_for_user(alice, taken) == "Alice 2"
    # The name is unique by id as well as by name: "Home" is taken however it is spelled.
    homely = RequestIdentity(owner=False, user_id="user-h", email="h@example.com", display_name="home!")
    assert desktop_name_for_user(homely, [home]) == "home! 2"


def test_the_next_glyph_is_the_first_unused_then_cycles() -> None:
    assert next_glyph_index([]) == 0
    assert next_glyph_index([0, 1, 3]) == 2
    assert next_glyph_index(list(range(GLYPH_COUNT))) == 0
    assert next_glyph_index([*range(GLYPH_COUNT), 0]) == 1
    assert len(DESKTOP_GLYPH_COLORS) == GLYPH_COUNT
