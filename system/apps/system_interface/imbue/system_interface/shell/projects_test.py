from pathlib import Path

import pytest
from app_manifest.manifest import ShortcutMode
from app_manifest.primitives import ActionId
from app_manifest.primitives import AppName
from app_manifest.registry import read_registry

from imbue.system_interface.shell.data_types import Shortcut
from imbue.system_interface.shell.errors import ProjectConflictError
from imbue.system_interface.shell.errors import ProjectNotFoundError
from imbue.system_interface.shell.errors import ProjectValueError
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import EVERYTHING_VIEW_ID
from imbue.system_interface.shell.projects import PROJECTS_FILENAME
from imbue.system_interface.shell.projects import ProjectStore
from imbue.system_interface.shell.projects import project_wire_json
from imbue.system_interface.shell.projects import seed_shortcuts
from imbue.system_interface.shell.projects import slugify_project_name
from imbue.system_interface.shell.projects import validated_shortcut
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry

_FILES = Address("app:files")
_TERMINAL_1 = Address("app:terminal?instance=terminal-1")
_SHORTCUT = Shortcut(app=AppName("terminal"), action=ActionId("new"), mode=ShortcutMode.NEW)


def _store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(state_directory=tmp_path / "state")


def test_slugs_shorten_names_and_refuse_empty_ones() -> None:
    assert slugify_project_name("  Research & Notes ") == "research-notes"
    with pytest.raises(ProjectValueError):
        slugify_project_name("***")


def test_create_lists_and_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.create_project("Research", "#12B5A5", 4, (_SHORTCUT,))
    assert created.id == "research"
    assert created.tabs == ()
    assert created.shortcuts == (_SHORTCUT,)
    assert [project.id for project in store.list_projects()] == ["research"]
    assert store.is_view_known("research") and store.is_view_known(EVERYTHING_VIEW_ID)
    assert not store.is_view_known("other")
    with pytest.raises(ProjectConflictError):
        store.create_project("research!", "#000000", 0, ())
    assert (tmp_path / "state" / PROJECTS_FILENAME).is_file()


@pytest.mark.parametrize(
    ("name", "color", "glyph"),
    [("", "#123456", 0), ("Ok", "red", 0), ("Ok", "#123456", 10), ("Ok", "#123456", -1)],
)
def test_bad_metadata_is_refused(tmp_path: Path, name: str, color: str, glyph: int) -> None:
    with pytest.raises(ProjectValueError):
        _store(tmp_path).create_project(name, color, glyph, ())


def test_settings_keep_the_id_tabs_and_shortcuts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_project("Alpha", "#111111", 1, (_SHORTCUT,))
    store.add_tab("alpha", _FILES)
    updated = store.update_project_settings("alpha", "Alpha Two", "#222222", 2)
    assert (updated.id, updated.name, updated.color, updated.glyph) == ("alpha", "Alpha Two", "#222222", 2)
    assert updated.tabs == (_FILES,)
    assert updated.shortcuts == (_SHORTCUT,)
    with pytest.raises(ProjectNotFoundError):
        store.update_project_settings("missing", "x", "#222222", 2)


def test_delete_reports_the_fallback_view(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_project("Alpha", "#111111", 1, ())
    store.create_project("Beta", "#111111", 1, ())
    assert store.delete_project("alpha") == "beta"
    assert store.delete_project("beta") == EVERYTHING_VIEW_ID
    with pytest.raises(ProjectNotFoundError):
        store.delete_project("beta")


def test_tab_sets_are_ordered_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_project("Alpha", "#111111", 1, ())
    store.add_tab("alpha", _TERMINAL_1)
    store.add_tab("alpha", _FILES)
    assert store.add_tab("alpha", _TERMINAL_1).tabs == (_TERMINAL_1, _FILES)
    assert store.remove_tab("alpha", _TERMINAL_1).tabs == (_FILES,)
    assert store.referenced_addresses() == {_FILES}


def test_shortcuts_replace_by_app_and_action(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_project("Alpha", "#111111", 1, (_SHORTCUT,))
    flipped = Shortcut(app=AppName("terminal"), action=ActionId("new"), mode=ShortcutMode.FOCUS)
    assert store.set_shortcut("alpha", flipped).shortcuts == (flipped,)
    other = Shortcut(app=AppName("files"), action=ActionId("open"), mode=ShortcutMode.FOCUS)
    assert store.set_shortcut("alpha", other).shortcuts == (flipped, other)
    assert store.remove_shortcut("alpha", "terminal", "new").shortcuts == (other,)


def test_addresses_no_app_lists_leave_every_tab_set(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_project("Alpha", "#111111", 1, ())
    store.create_project("Beta", "#111111", 1, ())
    store.add_tab("alpha", _TERMINAL_1)
    store.add_tab("beta", _FILES)
    assert store.remove_addresses_everywhere([_TERMINAL_1]) == ["alpha"]
    assert store.get_project("alpha").tabs == ()
    assert store.get_project("beta").tabs == (_FILES,)


def test_an_unreadable_file_reads_as_no_projects(tmp_path: Path) -> None:
    store = _store(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / PROJECTS_FILENAME).write_text('{"version": 1, "projects": "nope"}')
    assert store.list_projects() == []


def test_seed_shortcuts_follow_the_registry_and_skip_internal_rows(tmp_path: Path) -> None:
    rows = read_registry(
        write_registry(
            tmp_path / "apps.toml",
            registry_row_toml(
                "terminal", "http://localhost:1", True, actions=[("new", "New")], default_shortcut=("new", "new")
            ),
            registry_row_toml("files", "http://localhost:2", default_shortcut=("open", "focus")),
            registry_row_toml("secret", "http://localhost:3", is_internal=True, default_shortcut=("open", "focus")),
            registry_row_toml("plain", "http://localhost:4"),
        )
    )
    assert seed_shortcuts(rows) == (
        Shortcut(app=AppName("terminal"), action=ActionId("new"), mode=ShortcutMode.NEW),
        Shortcut(app=AppName("files"), action=ActionId("open"), mode=ShortcutMode.FOCUS),
    )
    terminal_row, files_row = rows[0], rows[1]
    assert validated_shortcut(_SHORTCUT, terminal_row) == _SHORTCUT
    with pytest.raises(ProjectValueError):
        validated_shortcut(
            Shortcut(app=AppName("terminal"), action=ActionId("open"), mode=ShortcutMode.NEW), terminal_row
        )
    with pytest.raises(ProjectValueError):
        validated_shortcut(_SHORTCUT, None)
    files_open = Shortcut(app=AppName("files"), action=ActionId("open"), mode=ShortcutMode.FOCUS)
    assert validated_shortcut(files_open, files_row) == files_open


def test_the_wire_shape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project = store.create_project("Alpha", "#111111", 1, (_SHORTCUT,))
    store.add_tab("alpha", _FILES)
    assert project_wire_json(store.get_project(project.id)) == {
        "id": "alpha",
        "name": "Alpha",
        "color": "#111111",
        "glyph": 1,
        "tabs": ["app:files"],
        "shortcuts": [{"app": "terminal", "action": "new", "mode": "new"}],
    }
