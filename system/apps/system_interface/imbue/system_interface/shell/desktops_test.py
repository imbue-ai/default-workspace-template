import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from app_manifest.manifest import Pin
from app_manifest.manifest import ShortcutMode
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from app_manifest.primitives import LaunchPathValue
from app_manifest.registry import read_registry

from imbue.imbue_common.model_update import to_update
from imbue.system_interface.shell.data_types import AppPin
from imbue.system_interface.shell.data_types import ClientRecord
from imbue.system_interface.shell.data_types import DesktopShortcut
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
from imbue.system_interface.shell.errors import ShellStateError
from imbue.system_interface.shell.errors import WindowNotFoundError
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import DesktopTheme
from imbue.system_interface.shell.primitives import GLYPH_COUNT
from imbue.system_interface.shell.primitives import UserId
from imbue.system_interface.shell.primitives import WallpaperKind
from imbue.system_interface.shell.primitives import WallpaperName
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.shell.testing import BUILTIN_SHORTCUT_APPS_WITH_CHAT
from imbue.system_interface.shell.testing import TEST_NOW
from imbue.system_interface.shell.testing import builtin_chat_row_toml
from imbue.system_interface.shell.testing import builtin_registry_rows
from imbue.system_interface.shell.testing import builtin_rows_toml_before_chat
from imbue.system_interface.shell.testing import read_default_shortcuts_offered
from imbue.system_interface.shell.testing import read_desktops_file_in_its_released_shape
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import shortcut_apps_on
from imbue.system_interface.shell.testing import window_record
from imbue.system_interface.shell.testing import write_desktops_file
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
    assert read_default_shortcuts_offered(tmp_path) == {"version": 1, "apps": ["chat"]}


def test_a_late_apps_default_shortcut_is_offered_once_on_every_desktop_and_a_removal_sticks(tmp_path: Path) -> None:
    before_chat, with_chat = builtin_registry_rows(tmp_path / "registry")
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
    assert (shortcut_apps_on(work)[-1], work.shortcuts[-1].cell) == ("chat", next_shortcut_cell(rearranged))
    assert store.list_desktops() == list(offered.desktops)
    assert read_default_shortcuts_offered(tmp_path) == {
        "version": 1,
        "apps": sorted(BUILTIN_SHORTCUT_APPS_WITH_CHAT),
    }
    stamp = (tmp_path / "desktops.json").stat().st_mtime_ns
    assert store.ensure_default_shortcuts_offered(with_chat).is_written is False
    assert (tmp_path / "desktops.json").stat().st_mtime_ns == stamp

    # A shortcut the user removes stays removed, the app's rows deregistered and registered again included.
    store.remove_shortcut("home", AppName("chat"), LaunchPathId("root"))
    for rows in (with_chat, before_chat, with_chat):
        assert store.ensure_default_shortcuts_offered(rows).is_written is False
    assert "chat" not in shortcut_apps_on(store.list_desktops()[0])


def test_a_workspaces_own_apps_are_added_once_to_every_desktop_and_a_removal_sticks(tmp_path: Path) -> None:
    """A workspace whose desktops were made before its user-built apps had shortcuts: each app a program runs is
    added once to every desktop, a preview frame is not, and a shortcut the user then removes stays removed."""
    _, with_chat = builtin_registry_rows(tmp_path / "registry")
    with_own_apps = read_registry(
        write_registry(
            tmp_path / "registry" / "with_own_apps.toml",
            *builtin_rows_toml_before_chat(),
            builtin_chat_row_toml(),
            registry_row_toml("notes", "http://localhost:8100", program="notes"),
            # An app from before manifests: registered with ``--name --icon-file --program``, no display name.
            registry_row_toml("recipes", "http://localhost:8200", program="recipes"),
            registry_row_toml("preview-1", "http://localhost:8300", display_name="Notes (preview)"),
        )
    )
    store = DesktopStore(state_directory=tmp_path / "state")
    store.ensure_default(lambda: seed_desktop_shortcuts(with_chat))
    store.create_desktop("Work", "#111111", 1, seed_desktop_shortcuts(with_chat), ())

    offered = store.ensure_default_shortcuts_offered(with_own_apps)

    assert offered.is_written is True
    for desktop in offered.desktops:
        assert shortcut_apps_on(desktop) == (*BUILTIN_SHORTCUT_APPS_WITH_CHAT, "notes", "recipes")
    assert read_default_shortcuts_offered(tmp_path / "state")["apps"] == sorted(
        (*BUILTIN_SHORTCUT_APPS_WITH_CHAT, "notes", "recipes")
    )

    store.remove_shortcut("home", AppName("notes"), LaunchPathId("open"))
    assert store.ensure_default_shortcuts_offered(with_own_apps).is_written is False
    assert "notes" not in shortcut_apps_on(store.list_desktops()[0])


@pytest.mark.parametrize("offered_record_text", [None, '{"version": 2, "apps": []}', '{"version": 1}', "not json"])
def test_a_desktops_file_with_no_usable_offered_record_counts_its_shortcuts_as_offered(
    tmp_path: Path, offered_record_text: str | None
) -> None:
    """A workspace whose default desktop was seeded before the chat registered, by a shell that kept no record or
    beside a record this shell cannot read: the chat is added once, as if registered first, the record is written,
    and desktops.json keeps the shape every release reads."""
    before_chat, with_chat = builtin_registry_rows(tmp_path / "registry")
    state_directory = tmp_path / "state"
    write_desktops_file(state_directory, default_desktop(seed_desktop_shortcuts(before_chat)))
    if offered_record_text is not None:
        (state_directory / DEFAULT_SHORTCUTS_OFFERED_FILENAME).write_text(offered_record_text)
    store = DesktopStore(state_directory=state_directory)

    offered = store.ensure_default_shortcuts_offered(with_chat)

    assert offered.is_written is True
    assert shortcut_apps_on(offered.desktops[0]) == BUILTIN_SHORTCUT_APPS_WITH_CHAT
    assert read_default_shortcuts_offered(state_directory)["apps"] == sorted(BUILTIN_SHORTCUT_APPS_WITH_CHAT)
    assert read_desktops_file_in_its_released_shape(state_directory) == offered.desktops


def test_a_created_or_added_desktop_records_the_apps_of_its_default_shortcuts(tmp_path: Path) -> None:
    store = DesktopStore(state_directory=tmp_path)
    store.create_desktop("Alpha", "#111111", 1, _SEED, ())
    assert read_default_shortcuts_offered(tmp_path)["apps"] == ["chat"]
    store.add_desktop(default_desktop(()), {AppName("files")})
    assert read_default_shortcuts_offered(tmp_path)["apps"] == ["chat", "files"]


def test_a_desktop_added_before_any_offered_record_starts_it_from_the_shortcuts_already_there(tmp_path: Path) -> None:
    store = DesktopStore(state_directory=tmp_path)
    store.ensure_default(lambda: _SEED)
    (tmp_path / DEFAULT_SHORTCUTS_OFFERED_FILENAME).unlink()

    store.create_desktop("Alpha", "#111111", 1, (), ())

    assert read_default_shortcuts_offered(tmp_path)["apps"] == ["chat"]


def _delete_file(path: Path) -> None:
    path.unlink()


def _add_an_unknown_desktop_key(path: Path) -> None:
    raw = json.loads(path.read_text())
    raw["desktops"][0]["unknown_key"] = True
    path.write_text(json.dumps(raw))


def _set_another_version(path: Path) -> None:
    raw = json.loads(path.read_text())
    raw["version"] = 2
    path.write_text(json.dumps(raw))


@pytest.mark.parametrize("spoil_desktops_file", [_delete_file, _add_an_unknown_desktop_key, _set_another_version])
def test_seeding_the_default_desktop_again_starts_the_offered_record_over(
    tmp_path: Path, spoil_desktops_file: Callable[[Path], None]
) -> None:
    """A desktops.json read as absent is seeded over before the chat registers: the record left from the desktops it
    replaced no longer names the chat as offered, so the chat still reaches the new default desktop."""
    before_chat, with_chat = builtin_registry_rows(tmp_path / "registry")
    state_directory = tmp_path / "state"
    store = DesktopStore(state_directory=state_directory)
    store.ensure_default(lambda: seed_desktop_shortcuts(before_chat))
    store.ensure_default_shortcuts_offered(with_chat)
    assert read_default_shortcuts_offered(state_directory)["apps"] == sorted(BUILTIN_SHORTCUT_APPS_WITH_CHAT)
    spoil_desktops_file(state_directory / "desktops.json")

    (home,) = store.ensure_default(lambda: seed_desktop_shortcuts(before_chat))

    assert read_default_shortcuts_offered(state_directory)["apps"] == sorted(shortcut_apps_on(home))
    offered = store.ensure_default_shortcuts_offered(with_chat)
    assert offered.is_written is True
    assert offered.desktops[0].shortcuts == seed_desktop_shortcuts(with_chat)


def test_a_desktop_added_where_no_desktop_stands_starts_the_offered_record_over(tmp_path: Path) -> None:
    store = DesktopStore(state_directory=tmp_path)
    store.ensure_default(lambda: _SEED)
    (tmp_path / "desktops.json").unlink()

    store.create_desktop("Alpha", "#111111", 1, (), ())

    assert read_default_shortcuts_offered(tmp_path)["apps"] == []


def _seed_the_default_desktop(store: DesktopStore) -> None:
    store.ensure_default(lambda: _SEED)


def _create_a_desktop(store: DesktopStore) -> None:
    store.create_desktop("Alpha", "#111111", 1, _SEED, ())


@pytest.mark.parametrize("make_a_desktop_where_none_stands", [_seed_the_default_desktop, _create_a_desktop])
def test_a_desktop_made_where_none_stands_is_not_written_when_its_offered_record_cannot_be(
    tmp_path: Path, make_a_desktop_where_none_stands: Callable[[DesktopStore], None]
) -> None:
    """The record starts over before desktops.json is written, so the record of replaced desktops never stays beside
    a new one."""
    store = DesktopStore(state_directory=tmp_path)
    # A directory where the offered record goes makes every write of it fail.
    (tmp_path / DEFAULT_SHORTCUTS_OFFERED_FILENAME).mkdir()

    with pytest.raises(ShellStateError):
        make_a_desktop_where_none_stands(store)

    assert not (tmp_path / "desktops.json").exists()


def test_with_no_desktop_nothing_is_offered_or_recorded(tmp_path: Path) -> None:
    _, with_chat = builtin_registry_rows(tmp_path / "registry")
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
    assert store.read_themes() == {}
    assert store.set_theme("research", DesktopTheme.WINDOWS_2000).name == "Research 2"
    assert store.read_themes() == {"research": DesktopTheme.WINDOWS_2000}
    # desktops.json keeps the shape a shell from before themes reads; the theme lives beside it.
    read_desktops_file_in_its_released_shape(store.state_directory)
    store.set_theme("research", DesktopTheme.DEFAULT)
    assert store.read_themes() == {}
    with pytest.raises(DesktopNotFoundError):
        store.set_theme("nowhere", DesktopTheme.MAC_CLASSIC)
    assert find_desktop_by_name_or_id(store.list_desktops(), "research 2") is not None
    assert find_desktop_by_name_or_id(store.list_desktops(), "nowhere") is None

    store.set_theme("home", DesktopTheme.MAC_CLASSIC)
    outcome = store.delete_desktop("home")
    assert outcome.deleted.id == home.id and outcome.fallback_desktop_id == "research"
    assert "home" not in store.read_themes()
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


@pytest.mark.frontend
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
