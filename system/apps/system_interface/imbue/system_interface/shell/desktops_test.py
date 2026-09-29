import json
import re
from pathlib import Path

import pytest
from app_manifest.manifest import Pin
from app_manifest.manifest import ShortcutMode
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from app_manifest.primitives import LaunchPathValue
from app_manifest.registry import RegistryRow
from app_manifest.registry import read_registry

from imbue.imbue_common.model_update import to_update
from imbue.system_interface.shell.data_types import AppPin
from imbue.system_interface.shell.data_types import ClientRecord
from imbue.system_interface.shell.data_types import Desktop
from imbue.system_interface.shell.data_types import DesktopShortcut
from imbue.system_interface.shell.data_types import DesktopsDocument
from imbue.system_interface.shell.data_types import GridCell
from imbue.system_interface.shell.data_types import ShortcutTarget
from imbue.system_interface.shell.data_types import Wallpaper
from imbue.system_interface.shell.desktop_document import next_shortcut_cell
from imbue.system_interface.shell.desktop_document import seed_desktop_shortcuts
from imbue.system_interface.shell.desktops import DEFAULT_SHORTCUTS_OFFERED_FILENAME
from imbue.system_interface.shell.desktops import DESKTOP_GLYPH_COLORS
from imbue.system_interface.shell.desktops import DesktopStore
from imbue.system_interface.shell.desktops import FALLBACK_USER_DESKTOP_NAME
from imbue.system_interface.shell.desktops import default_desktop
from imbue.system_interface.shell.desktops import desktop_kept_by_returning_client
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
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import GLYPH_COUNT
from imbue.system_interface.shell.primitives import UserId
from imbue.system_interface.shell.primitives import WallpaperKind
from imbue.system_interface.shell.primitives import WallpaperName
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.shell.testing import TEST_NOW
from imbue.system_interface.shell.testing import builtin_chat_row_toml
from imbue.system_interface.shell.testing import builtin_rows_toml_before_chat
from imbue.system_interface.shell.testing import window_record
from imbue.system_interface.shell.testing import write_registry

# The frontend's glyph palette, which ``DESKTOP_GLYPH_COLORS`` restates for the desktops the shell names itself.
_SQUIGGLES_PATH = Path(__file__).resolve().parents[3] / "frontend" / "src" / "views" / "squiggles.ts"
_SQUIGGLE_GLYPHS_ARRAY = re.compile(r"export const SQUIGGLE_GLYPHS\b[^=]*=\s*\[(.*?)\n\];", re.DOTALL)
_GLYPH_COLOR = re.compile(r'color: "(#[0-9A-Fa-f]{6})"')

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
    # The apps of the seeded shortcuts are recorded as offered.
    assert _offered_record(tmp_path) == {"version": 1, "apps": ["chat"]}


def _offered_record(state_directory: Path) -> dict[str, object]:
    return json.loads((state_directory / DEFAULT_SHORTCUTS_OFFERED_FILENAME).read_text())


def _builtin_rows(tmp_path: Path) -> tuple[list[RegistryRow], list[RegistryRow]]:
    """The built-in apps' rows without the chat, and with it registered last."""
    before_chat = read_registry(write_registry(tmp_path / "before.toml", *builtin_rows_toml_before_chat()))
    with_chat = read_registry(
        write_registry(tmp_path / "with_chat.toml", *builtin_rows_toml_before_chat(), builtin_chat_row_toml())
    )
    return before_chat, with_chat


def _apps_on(desktop: Desktop) -> list[str]:
    return [str(shortcut.target.app) for shortcut in desktop.shortcuts]


def test_a_late_apps_default_shortcut_is_offered_once_on_every_desktop_and_a_removal_sticks(tmp_path: Path) -> None:
    before_chat, with_chat = _builtin_rows(tmp_path / "registry")
    store = DesktopStore(state_directory=tmp_path)
    store.ensure_default(lambda: seed_desktop_shortcuts(before_chat))
    store.create_desktop("Work", "#111111", 1, seed_desktop_shortcuts(before_chat), ())
    rearranged = store.move_shortcut("work", AppName("files"), LaunchPathId("new"), GridCell(column=0, row=3))
    assert store.ensure_default_shortcuts_offered(before_chat).is_written is False

    offered = store.ensure_default_shortcuts_offered(with_chat)

    assert offered.is_written is True
    home, work = offered.desktops
    # Home was untouched, so it reads as if the chat had registered first; Work keeps its arrangement.
    assert home.shortcuts == seed_desktop_shortcuts(with_chat)
    assert work.shortcuts[:-1] == rearranged.shortcuts
    assert (_apps_on(work)[-1], work.shortcuts[-1].cell) == ("chat", next_shortcut_cell(rearranged))
    assert store.list_desktops() == list(offered.desktops)
    assert _offered_record(tmp_path) == {
        "version": 1,
        "apps": ["browser", "chat", "files", "getting-started", "terminal"],
    }
    stamp = (tmp_path / "desktops.json").stat().st_mtime_ns
    assert store.ensure_default_shortcuts_offered(with_chat).is_written is False
    assert (tmp_path / "desktops.json").stat().st_mtime_ns == stamp

    # A shortcut the user removes stays removed, the app's rows deregistered and registered again included.
    store.remove_shortcut("home", AppName("chat"), LaunchPathId("root"))
    for rows in (with_chat, before_chat, with_chat):
        assert store.ensure_default_shortcuts_offered(rows).is_written is False
    assert "chat" not in _apps_on(store.list_desktops()[0])


def test_a_desktops_file_from_before_the_offered_record_counts_its_shortcuts_as_offered(tmp_path: Path) -> None:
    """A workspace whose default desktop was seeded before the chat registered, by a shell that kept no record: the
    chat is added once, the record is created, and desktops.json keeps the shape every release reads."""
    before_chat, with_chat = _builtin_rows(tmp_path / "registry")
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    (state_directory / "desktops.json").write_text(
        json.dumps(
            DesktopsDocument(version=1, desktops=(default_desktop(seed_desktop_shortcuts(before_chat)),)).model_dump(
                mode="json"
            )
        )
    )
    store = DesktopStore(state_directory=state_directory)

    offered = store.ensure_default_shortcuts_offered(with_chat)

    assert offered.is_written is True
    assert _apps_on(offered.desktops[0]) == ["chat", "getting-started", "files", "browser", "terminal"]
    assert _offered_record(state_directory)["apps"] == ["browser", "chat", "files", "getting-started", "terminal"]
    raw = json.loads((state_directory / "desktops.json").read_text())
    assert set(raw) == {"version", "desktops"} and raw["version"] == 1
    assert set(raw["desktops"][0]) == {"id", "name", "color", "glyph", "wallpaper", "shortcuts", "windows"}
    assert DesktopsDocument.model_validate(raw).desktops == offered.desktops


def test_a_created_or_added_desktop_records_the_apps_of_its_default_shortcuts(tmp_path: Path) -> None:
    store = DesktopStore(state_directory=tmp_path)
    store.create_desktop("Alpha", "#111111", 1, _SEED, ())
    assert _offered_record(tmp_path)["apps"] == ["chat"]
    store.add_desktop(default_desktop(()), {AppName("files")})
    assert _offered_record(tmp_path)["apps"] == ["chat", "files"]


def test_a_desktop_added_before_any_offered_record_starts_it_from_the_shortcuts_already_there(tmp_path: Path) -> None:
    store = DesktopStore(state_directory=tmp_path)
    store.ensure_default(lambda: _SEED)
    (tmp_path / DEFAULT_SHORTCUTS_OFFERED_FILENAME).unlink()

    store.create_desktop("Alpha", "#111111", 1, (), ())

    assert _offered_record(tmp_path)["apps"] == ["chat"]


def test_with_no_desktop_nothing_is_offered_or_recorded(tmp_path: Path) -> None:
    _, with_chat = _builtin_rows(tmp_path / "registry")
    store = DesktopStore(state_directory=tmp_path / "state")

    outcome = store.ensure_default_shortcuts_offered(with_chat)

    assert outcome.is_written is False and outcome.desktops == ()
    assert not (tmp_path / "state").exists()


def test_pinned_windows_are_reconciled_across_every_desktop_and_written_only_on_a_change(tmp_path: Path) -> None:
    store = DesktopStore(state_directory=tmp_path)
    store.ensure_default(lambda: ())
    store.create_desktop("Alpha", "#111111", 1, (), ())
    pin = AppPin(app=AppName("chat"), pin=Pin(path=LaunchPathValue("/")))
    ensured = store.ensure_pinned_windows([pin], TEST_NOW)
    assert ensured.is_written is True
    assert [[str(window.app) for window in desktop.windows] for desktop in ensured.desktops] == [["chat"], ["chat"]]
    assert all(desktop.windows[0].is_pinned for desktop in ensured.desktops)
    stamp = (tmp_path / "desktops.json").stat().st_mtime_ns
    again = store.ensure_pinned_windows([pin], TEST_NOW)
    assert again.is_written is False and again.desktops == ensured.desktops
    assert (tmp_path / "desktops.json").stat().st_mtime_ns == stamp
    withdrawn = store.ensure_pinned_windows([], TEST_NOW)
    assert withdrawn.is_written is True
    assert all(desktop.windows[0].is_pinned is False for desktop in withdrawn.desktops)
    # A pinned window is born with a new desktop when the caller hands it over.
    born = store.create_desktop("Beta", "#222222", 2, (), (ensured.desktops[0].windows[0],))
    assert born.windows == (ensured.desktops[0].windows[0],)


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
    research = store.create_desktop("Research!", "#12B5A5", 4, _SEED, ())
    assert research.id == "research" and research.shortcuts == _SEED
    with pytest.raises(DesktopConflictError):
        store.create_desktop("research", "#12B5A5", 4, (), ())
    with pytest.raises(DesktopValueError):
        store.create_desktop("Bad", "red", 4, (), ())
    with pytest.raises(DesktopValueError):
        store.create_desktop("Bad", "#12B5A5", 12, (), ())
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
    opened = store.open_window("home", window_record(_WIN_1, "chat", "/"))
    assert [window.id for window in opened.windows] == [_WIN_1]
    located = store.set_window_location("home", _WIN_1, WindowPath("/?chat=agent-1"), WindowTitle("Plan"))
    assert located.is_written is True and located.desktop.windows[0].path == "/?chat=agent-1"
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
    home, alpha = store.ensure_default(lambda: ())[0], store.create_desktop("Alpha", "#111111", 1, (), ())
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


def test_a_desktops_file_carrying_the_retired_window_settling_key_still_reads(tmp_path: Path) -> None:
    store = DesktopStore(state_directory=tmp_path)
    store.ensure_default(lambda: ())
    opened = store.open_window("home", window_record(_WIN_1, "chat", "/"))
    raw = json.loads((tmp_path / "desktops.json").read_text())
    raw["desktops"][0]["windows"][0]["is_settling"] = True
    (tmp_path / "desktops.json").write_text(json.dumps(raw))
    assert store.list_desktops() == [opened]
    store.update_settings("home", "Home", "#2f6b4f", 1)
    assert "is_settling" not in json.loads((tmp_path / "desktops.json").read_text())["desktops"][0]["windows"][0]


def test_a_users_desktop_is_named_after_them_and_made_unique() -> None:
    home = default_desktop(())
    assert desktop_name_for_user("Alice", "alice@example.com", [home]) == "Alice"
    assert desktop_name_for_user(None, "bob.smith@example.com", [home]) == "bob.smith"
    assert desktop_name_for_user("   ", "!!!@example.com", [home]) == FALLBACK_USER_DESKTOP_NAME
    assert desktop_name_for_user(None, None, [home]) == FALLBACK_USER_DESKTOP_NAME
    taken = [
        home,
        home.model_copy_update(
            to_update(home.field_ref().id, DesktopId("alice")), to_update(home.field_ref().name, "alice")
        ),
    ]
    assert desktop_name_for_user("Alice", "alice@example.com", taken) == "Alice 2"
    # The name is unique by id as well as by name: "Home" is taken however it is spelled.
    assert desktop_name_for_user("home!", "h@example.com", [home]) == "home! 2"


def test_the_next_glyph_is_the_first_unused_then_cycles() -> None:
    assert next_glyph_index([]) == 0
    assert next_glyph_index([0, 1, 3]) == 2
    assert next_glyph_index(list(range(GLYPH_COUNT))) == 0
    assert next_glyph_index([*range(GLYPH_COUNT), 0]) == 1


def test_the_shells_glyph_colours_are_the_frontends_palette_in_glyph_order() -> None:
    palette = _SQUIGGLE_GLYPHS_ARRAY.search(_SQUIGGLES_PATH.read_text())
    assert palette is not None, f"no SQUIGGLE_GLYPHS array in {_SQUIGGLES_PATH}"
    assert tuple(_GLYPH_COLOR.findall(palette.group(1))) == DESKTOP_GLYPH_COLORS
    assert len(DESKTOP_GLYPH_COLORS) == GLYPH_COUNT


def test_a_returning_client_keeps_its_desktop_only_when_it_last_arrived_as_the_same_user() -> None:
    alice = UserId("user-alice")
    desktop_ids = {DesktopId("home"), DesktopId("alice")}
    on_home = ClientRecord(id=ClientId("c1"), active_desktop=DesktopId("home"), last_seen=TEST_NOW, user_id=alice)
    assert desktop_kept_by_returning_client(on_home, alice, desktop_ids) == "home"
    assert desktop_kept_by_returning_client(None, alice, desktop_ids) is None
    bobs = on_home.model_copy_update(to_update(on_home.field_ref().user_id, UserId("user-bob")))
    assert desktop_kept_by_returning_client(bobs, alice, desktop_ids) is None
    anonymous = on_home.model_copy_update(to_update(on_home.field_ref().user_id, None))
    assert desktop_kept_by_returning_client(anonymous, alice, desktop_ids) is None
    gone = on_home.model_copy_update(to_update(on_home.field_ref().active_desktop, DesktopId("deleted")))
    assert desktop_kept_by_returning_client(gone, alice, desktop_ids) is None
