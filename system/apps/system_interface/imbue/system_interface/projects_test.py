import json
from pathlib import Path
from typing import Any

import pytest

from imbue.system_interface.projects import DEFAULT_PROJECT_COLOR
from imbue.system_interface.projects import DEFAULT_PROJECT_ID
from imbue.system_interface.projects import DEFAULT_PROJECT_NAME
from imbue.system_interface.projects import EVERYTHING_VIEW_ID
from imbue.system_interface.projects import ProjectColorError
from imbue.system_interface.projects import ProjectConflictError
from imbue.system_interface.projects import ProjectDeviceError
from imbue.system_interface.projects import ProjectGlyphError
from imbue.system_interface.projects import ProjectMemberRefError
from imbue.system_interface.projects import ProjectNameError
from imbue.system_interface.projects import ProjectNotFoundError
from imbue.system_interface.projects import ProjectShortcutError
from imbue.system_interface.projects import add_member
from imbue.system_interface.projects import all_members
from imbue.system_interface.projects import content_contains_panel
from imbue.system_interface.projects import create_project
from imbue.system_interface.projects import delete_project
from imbue.system_interface.projects import get_last_active_id
from imbue.system_interface.projects import list_members
from imbue.system_interface.projects import list_projects
from imbue.system_interface.projects import member_refs_from_content
from imbue.system_interface.projects import project_content_path
from imbue.system_interface.projects import projects_showing
from imbue.system_interface.projects import read_project_content
from imbue.system_interface.projects import remove_member
from imbue.system_interface.projects import remove_panel_from_all_projects
from imbue.system_interface.projects import set_last_active_id
from imbue.system_interface.projects import set_shortcut_override
from imbue.system_interface.projects import slugify_project_name
from imbue.system_interface.projects import strip_panel_from_content
from imbue.system_interface.projects import update_project
from imbue.system_interface.projects import write_project_content


def _content_with_panels(*panel_ids: str) -> dict[str, Any]:
    """A minimal saved-content blob holding ``panel_ids`` in one leaf group."""
    return {
        "dockview": {
            "grid": {
                "root": {
                    "type": "branch",
                    "data": [
                        {
                            "type": "leaf",
                            "data": {"id": "group-1", "views": list(panel_ids), "activeView": panel_ids[0]},
                        }
                    ],
                },
                "width": 100,
                "height": 100,
                "orientation": "HORIZONTAL",
            },
            "panels": {panel_id: {"id": panel_id} for panel_id in panel_ids},
        },
        "panelParams": {panel_id: {"panelType": "chat"} for panel_id in panel_ids},
    }


def _write_legacy_desktop_layout(layout_dir: Path, content: dict[str, Any]) -> None:
    """Lay down a pre-projects named-layout store holding ``content`` as ``desktop``."""
    desktop_path = layout_dir / "layouts" / "desktop.json"
    desktop_path.parent.mkdir(parents=True, exist_ok=True)
    desktop_path.write_text(json.dumps(content, separators=(",", ":")))
    (layout_dir / "layouts_meta.json").write_text(
        json.dumps({"display_name_by_slug": {"desktop": "desktop"}, "last_active_slug": "desktop"})
    )


def test_slugify_project_name_normalizes() -> None:
    assert slugify_project_name("My Fancy Project!") == "my-fancy-project"
    assert slugify_project_name("  Everything  ") == "everything"
    assert slugify_project_name("a_b c") == "a-b-c"


def test_slugify_project_name_rejects_unusable() -> None:
    with pytest.raises(ProjectNameError):
        slugify_project_name("!!!")
    with pytest.raises(ProjectNameError):
        slugify_project_name("   ")


def test_the_default_project_id_is_the_slug_of_its_name() -> None:
    assert slugify_project_name(DEFAULT_PROJECT_NAME) == DEFAULT_PROJECT_ID


def test_defaults_seed_one_empty_starter_project(tmp_path: Path) -> None:
    infos = list_projects(tmp_path)
    assert [info.project_id for info in infos] == [DEFAULT_PROJECT_ID]
    starter = infos[0]
    assert starter.name == DEFAULT_PROJECT_NAME
    assert starter.color == DEFAULT_PROJECT_COLOR
    assert starter.glyph == 0
    assert starter.has_content is False
    assert starter.members == ()
    assert get_last_active_id(tmp_path) == DEFAULT_PROJECT_ID


def test_everything_is_not_a_stored_project(tmp_path: Path) -> None:
    # Everything is the unfiltered view, rendered by enumerating the machine;
    # nothing in the registry knows about it, so its id is free for a real
    # project (whose own content file is what that id then addresses).
    create_project(tmp_path, "Everything", "#12B5A5", 4)
    assert [info.project_id for info in list_projects(tmp_path)] == [DEFAULT_PROJECT_ID, "everything"]
    assert delete_project(tmp_path, "everything") == DEFAULT_PROJECT_ID


def test_create_project_round_trips(tmp_path: Path) -> None:
    info = create_project(tmp_path, "Data Pipeline", "#3B82F6", 6)
    assert info.project_id == "data-pipeline"
    assert info.name == "Data Pipeline"
    assert info.color == "#3B82F6"
    assert info.glyph == 6
    assert info.has_content is False
    assert info.members == ()

    listed = list_projects(tmp_path)
    assert [listed_info.project_id for listed_info in listed] == [DEFAULT_PROJECT_ID, "data-pipeline"]
    # A create is immediately followed by a switch in the UI, so it moves the pointer.
    assert get_last_active_id(tmp_path) == "data-pipeline"


def test_create_project_rejects_slug_collision(tmp_path: Path) -> None:
    create_project(tmp_path, "Data Pipeline", "#3B82F6", 6)
    with pytest.raises(ProjectConflictError):
        create_project(tmp_path, "data pipeline", "#16A34A", 1)
    with pytest.raises(ProjectConflictError):
        create_project(tmp_path, DEFAULT_PROJECT_NAME, "#16A34A", 1)


def test_update_project_keeps_id_content_and_members(tmp_path: Path) -> None:
    create_project(tmp_path, "Data Pipeline", "#3B82F6", 6)
    content = {"dockview": {"grid": {}}, "panelParams": {"p": {"panelType": "chat"}}}
    write_project_content(tmp_path, "data-pipeline", content)
    add_member(tmp_path, "data-pipeline", "terminal:build")

    updated = update_project(tmp_path, "data-pipeline", "Renamed Entirely", "#EC4899", 9)
    assert updated.project_id == "data-pipeline"
    assert updated.name == "Renamed Entirely"
    assert updated.color == "#EC4899"
    assert updated.glyph == 9
    assert updated.has_content is True
    # A rename is purely cosmetic: neither the content file nor the members move.
    assert updated.members == ("terminal:build",)
    assert read_project_content(tmp_path, "data-pipeline") == content
    assert [info.name for info in list_projects(tmp_path)] == [DEFAULT_PROJECT_NAME, "Renamed Entirely"]


def test_update_unknown_project_raises(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotFoundError):
        update_project(tmp_path, "ghost", "Ghost", "#3B82F6", 1)


def test_content_round_trip(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    assert read_project_content(tmp_path, "research") is None

    content = {"dockview": {"grid": {"root": {}}}, "panelParams": {"tab-1": {"panelType": "terminal"}}}
    write_project_content(tmp_path, "research", content)
    assert read_project_content(tmp_path, "research") == content
    assert project_content_path(tmp_path, "research").exists()
    # Writing content flags the project as non-empty but leaves the pointer alone.
    set_last_active_id(tmp_path, DEFAULT_PROJECT_ID)
    write_project_content(tmp_path, "research", content)
    assert get_last_active_id(tmp_path) == DEFAULT_PROJECT_ID


def test_content_access_for_unknown_project_raises(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotFoundError):
        read_project_content(tmp_path, "ghost")
    with pytest.raises(ProjectNotFoundError):
        write_project_content(tmp_path, "ghost", {})


def test_each_device_keeps_its_own_arrangement(tmp_path: Path) -> None:
    # One view, two files: a mobile save neither seeds nor disturbs desktop,
    # and desktop reads stay None until a desktop client saves there.
    create_project(tmp_path, "Research", "#12B5A5", 4)
    mobile_content = _content_with_panels("chat-m")
    write_project_content(tmp_path, "research", mobile_content, "mobile")
    assert read_project_content(tmp_path, "research", "mobile") == mobile_content
    assert read_project_content(tmp_path, "research", "desktop") is None
    assert not project_content_path(tmp_path, "research", "desktop").exists()

    desktop_content = _content_with_panels("chat-d")
    write_project_content(tmp_path, "research", desktop_content)
    assert read_project_content(tmp_path, "research") == desktop_content
    assert read_project_content(tmp_path, "research", "mobile") == mobile_content
    assert project_content_path(tmp_path, "research", "mobile").name == "research.mobile.json"
    assert project_content_path(tmp_path, "research").name == "research.json"


def test_unknown_device_kind_is_rejected(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    with pytest.raises(ProjectDeviceError):
        read_project_content(tmp_path, "research", "tablet")
    with pytest.raises(ProjectDeviceError):
        write_project_content(tmp_path, "research", {}, "tablet")
    with pytest.raises(ProjectDeviceError):
        project_content_path(tmp_path, "research", "tablet")


def test_autosave_does_not_touch_membership(tmp_path: Path) -> None:
    # Closing a tab rewrites the layout but must never unfile the object: the
    # member stays listed, backgrounded, until it is explicitly removed.
    create_project(tmp_path, "Research", "#12B5A5", 4)
    add_member(tmp_path, "research", "terminal:build")
    write_project_content(tmp_path, "research", _content_with_panels("chat-a"))
    assert list_members(tmp_path, "research") == ["terminal:build"]


def test_delete_project_returns_the_first_remaining_project(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    write_project_content(tmp_path, "research", {"dockview": {}, "panelParams": {}})
    set_last_active_id(tmp_path, "research")

    write_project_content(tmp_path, "research", {"dockview": {}, "panelParams": {}}, "mobile")
    fallback_id = delete_project(tmp_path, "research")
    assert fallback_id == DEFAULT_PROJECT_ID
    assert not project_content_path(tmp_path, "research").exists()
    assert not project_content_path(tmp_path, "research", "mobile").exists()
    assert [info.project_id for info in list_projects(tmp_path)] == [DEFAULT_PROJECT_ID]
    assert get_last_active_id(tmp_path) == DEFAULT_PROJECT_ID

    with pytest.raises(ProjectNotFoundError):
        delete_project(tmp_path, "research")


def test_delete_project_unfiles_its_members(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    add_member(tmp_path, "research", "service:notes")
    assert all_members(tmp_path) == {"service:notes": ["research"]}

    delete_project(tmp_path, "research")

    assert all_members(tmp_path) == {}
    assert projects_showing(tmp_path, "service:notes") == []
    add_member(tmp_path, DEFAULT_PROJECT_ID, "service:notes")
    assert projects_showing(tmp_path, "service:notes") == [DEFAULT_PROJECT_ID]


def test_deleting_the_last_project_leaves_the_machine_with_none(tmp_path: Path) -> None:
    # Delete is a pure view operation now, so there is no undeletable project:
    # a machine may end up with zero of them, with Everything as the fallback.
    fallback_id = delete_project(tmp_path, DEFAULT_PROJECT_ID)

    assert fallback_id == EVERYTHING_VIEW_ID
    assert list_projects(tmp_path) == []
    assert get_last_active_id(tmp_path) == EVERYTHING_VIEW_ID
    # Reading again does not resurrect the starter project: an empty registry
    # is a legitimate, persistent state rather than something to reseed.
    assert list_projects(tmp_path) == []
    with pytest.raises(ProjectNotFoundError):
        delete_project(tmp_path, DEFAULT_PROJECT_ID)


def test_everything_still_works_with_zero_projects(tmp_path: Path) -> None:
    # Everything has no registry entry, so it never depended on a project
    # existing; deleting every project must not take it down too.
    write_project_content(tmp_path, EVERYTHING_VIEW_ID, {"dockview": {}, "panelParams": {}})
    delete_project(tmp_path, DEFAULT_PROJECT_ID)

    assert read_project_content(tmp_path, EVERYTHING_VIEW_ID) == {"dockview": {}, "panelParams": {}}
    write_project_content(tmp_path, EVERYTHING_VIEW_ID, {"dockview": {"grid": {}}, "panelParams": {}})
    assert read_project_content(tmp_path, EVERYTHING_VIEW_ID) == {"dockview": {"grid": {}}, "panelParams": {}}


def test_deleting_a_project_never_touches_other_projects_or_everything(tmp_path: Path) -> None:
    # A pure view operation: the deleted project's own member list and content
    # go, and nothing about any other view -- not a member list, not a saved
    # arrangement -- moves.
    create_project(tmp_path, "Scratch", "#3B82F6", 3)
    add_member(tmp_path, "scratch", "terminal:terminal-4")
    add_member(tmp_path, DEFAULT_PROJECT_ID, "terminal:terminal-4")
    write_project_content(tmp_path, EVERYTHING_VIEW_ID, _content_with_panels("terminal-session-terminal-4"))

    delete_project(tmp_path, "scratch")

    # The surviving project keeps the member the deleted one also showed.
    assert list_members(tmp_path, DEFAULT_PROJECT_ID) == ["terminal:terminal-4"]
    assert projects_showing(tmp_path, "terminal:terminal-4") == [DEFAULT_PROJECT_ID]
    # Everything's saved arrangement is untouched: nothing was destroyed.
    assert content_contains_panel(
        read_project_content(tmp_path, EVERYTHING_VIEW_ID) or {}, "terminal-session-terminal-4"
    )


def test_set_last_active_ignores_unknown_id(tmp_path: Path) -> None:
    set_last_active_id(tmp_path, "ghost")
    assert get_last_active_id(tmp_path) == DEFAULT_PROJECT_ID


def test_last_active_pointer_moves_and_survives_a_stale_id(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    set_last_active_id(tmp_path, DEFAULT_PROJECT_ID)
    assert get_last_active_id(tmp_path) == DEFAULT_PROJECT_ID

    # A registry edited from outside can point at a project that no longer exists.
    (tmp_path / "projects_meta.json").write_text(
        '{"project_by_id": {"research": {"name": "Research", "color": "#12B5A5", "glyph": 4, "members": []}}, '
        '"last_active_id": "vanished"}'
    )
    assert get_last_active_id(tmp_path) == "research"


def test_color_must_be_hex_rrggbb(tmp_path: Path) -> None:
    for bad_color in ("blue", "#FFF", "#12345", "#GGGGGG", ""):
        with pytest.raises(ProjectColorError):
            create_project(tmp_path, "Research", bad_color, 0)
    # Case is preserved so the frontend's swatch comparison stays exact.
    assert create_project(tmp_path, "Research", "  #7c5cFF  ", 0).color == "#7c5cFF"
    with pytest.raises(ProjectColorError):
        update_project(tmp_path, "research", "Research", "not-a-color", 0)


def test_glyph_must_index_the_glyph_table(tmp_path: Path) -> None:
    for bad_glyph in (-1, 10, 99):
        with pytest.raises(ProjectGlyphError):
            create_project(tmp_path, "Research", "#3B82F6", bad_glyph)
    create_project(tmp_path, "Research", "#3B82F6", 9)
    with pytest.raises(ProjectGlyphError):
        update_project(tmp_path, "research", "Research", "#3B82F6", 10)


def test_blank_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProjectNameError):
        create_project(tmp_path, "   ", "#3B82F6", 0)
    with pytest.raises(ProjectNameError):
        update_project(tmp_path, DEFAULT_PROJECT_ID, "  ", "#3B82F6", 0)


def test_blank_member_ref_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProjectMemberRefError):
        add_member(tmp_path, DEFAULT_PROJECT_ID, "   ")
    with pytest.raises(ProjectMemberRefError):
        remove_member(tmp_path, DEFAULT_PROJECT_ID, "")
    with pytest.raises(ProjectMemberRefError):
        projects_showing(tmp_path, " ")


def test_corrupt_meta_recovers_to_defaults(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    (tmp_path / "projects_meta.json").write_text("not json{")
    infos = list_projects(tmp_path)
    assert [info.project_id for info in infos] == [DEFAULT_PROJECT_ID]
    assert get_last_active_id(tmp_path) == DEFAULT_PROJECT_ID


def test_registry_with_no_projects_is_read_as_is(tmp_path: Path) -> None:
    # Deleting is a pure view operation with no undeletable project any more, so
    # an on-disk registry holding zero of them is a legitimate state -- reached
    # by deleting down to none -- and must not be reseeded back to the starter
    # project on the next read.
    (tmp_path / "projects_meta.json").write_text('{"project_by_id": {}, "last_active_id": "gone"}')
    assert list_projects(tmp_path) == []
    assert get_last_active_id(tmp_path) == EVERYTHING_VIEW_ID


def test_legacy_url_members_are_purged_on_read_and_persisted(tmp_path: Path) -> None:
    # Ad-hoc pages are no longer filed as members, so url: refs written by the
    # old filer are dead entries; the first read drops them everywhere and
    # rewrites the registry, leaving every real member untouched.
    (tmp_path / "projects_meta.json").write_text(
        json.dumps(
            {
                "project_by_id": {
                    "research": {
                        "name": "Research",
                        "color": "#12B5A5",
                        "glyph": 4,
                        "members": ["chat:agent-1", "url:9f86d081", "service:web"],
                    },
                    "taxes": {"name": "Taxes", "color": "#E5A33D", "glyph": 7, "members": ["url:deadbeef"]},
                },
                "last_active_id": "research",
            }
        )
    )

    members_by_id = {info.project_id: list(info.members) for info in list_projects(tmp_path)}

    assert members_by_id == {"research": ["chat:agent-1", "service:web"], "taxes": []}
    persisted = json.loads((tmp_path / "projects_meta.json").read_text())
    assert persisted["project_by_id"]["research"]["members"] == ["chat:agent-1", "service:web"]
    assert persisted["project_by_id"]["taxes"]["members"] == []


def test_bare_files_members_are_purged_on_read_while_instances_survive(tmp_path: Path) -> None:
    # The file viewer's pin is its built-in rail row, never membership, so a
    # bare service:files ref (filed by builds whose opens filed bare app refs)
    # is a dead entry that only ever rendered a phantom "files" tab row. Its
    # instances -- and other apps' bare pins -- are real members and stay.
    (tmp_path / "projects_meta.json").write_text(
        json.dumps(
            {
                "project_by_id": {
                    "research": {
                        "name": "Research",
                        "color": "#12B5A5",
                        "glyph": 4,
                        "members": ["service:files", "service:files?instance=files-1", "service:docs"],
                    },
                },
                "last_active_id": "research",
            }
        )
    )

    members_by_id = {info.project_id: list(info.members) for info in list_projects(tmp_path)}

    assert members_by_id == {"research": ["service:files?instance=files-1", "service:docs"]}
    persisted = json.loads((tmp_path / "projects_meta.json").read_text())
    assert persisted["project_by_id"]["research"]["members"] == ["service:files?instance=files-1", "service:docs"]


def test_corrupt_content_reads_as_empty(tmp_path: Path) -> None:
    write_project_content(tmp_path, DEFAULT_PROJECT_ID, {"ok": True})
    project_content_path(tmp_path, DEFAULT_PROJECT_ID).write_text("garbage{")
    assert read_project_content(tmp_path, DEFAULT_PROJECT_ID) is None


def test_add_member_is_idempotent_and_ordered(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    add_member(tmp_path, "research", "chat:agent-1")
    add_member(tmp_path, "research", "terminal:build")
    add_member(tmp_path, "research", "chat:agent-1")
    assert list_members(tmp_path, "research") == ["chat:agent-1", "terminal:build"]
    assert projects_showing(tmp_path, "chat:agent-1") == ["research"]


def test_add_member_trims_the_ref(tmp_path: Path) -> None:
    add_member(tmp_path, DEFAULT_PROJECT_ID, "  service:notes  ")
    assert list_members(tmp_path, DEFAULT_PROJECT_ID) == ["service:notes"]


def test_one_object_can_show_in_several_projects(tmp_path: Path) -> None:
    # A project is a view, so the machine's one app can sit in every project
    # that cares about it. Adding it a second time takes it from nowhere.
    create_project(tmp_path, "Research", "#12B5A5", 4)
    create_project(tmp_path, "Coding", "#16A34A", 1)
    add_member(tmp_path, "research", "service:notes")
    add_member(tmp_path, "coding", "service:notes")

    assert list_members(tmp_path, "research") == ["service:notes"]
    assert list_members(tmp_path, "coding") == ["service:notes"]
    assert projects_showing(tmp_path, "service:notes") == ["research", "coding"]
    assert all_members(tmp_path) == {"service:notes": ["research", "coding"]}


def test_refs_in_no_project_are_filed_nowhere(tmp_path: Path) -> None:
    # An object nothing has filed still exists on the machine; Everything is
    # its home, and Everything enumerates the machine rather than this registry.
    assert projects_showing(tmp_path, "chat:loose-agent") == []
    assert all_members(tmp_path) == {}


def _overrides(layout_dir: Path, project_id: str) -> dict[str, dict[str, object]]:
    info = next(p for p in list_projects(layout_dir) if p.project_id == project_id)
    return {
        shortcut_id: override.model_dump(exclude_none=True)
        for shortcut_id, override in info.shortcut_overrides.items()
    }


def test_a_project_shows_every_shortcut_until_one_is_overridden(tmp_path: Path) -> None:
    """Absence has to mean the defaults, or every project written before this would lose its rail."""
    create_project(tmp_path, "Research", "#12B5A5", 4)
    assert _overrides(tmp_path, "research") == {}


def test_unpinning_a_shortcut_records_it_against_that_project_only(tmp_path: Path) -> None:
    # Which starting points a project keeps to hand is a property of THAT
    # project, which is the whole reason this is stored per project.
    create_project(tmp_path, "Research", "#12B5A5", 4)
    create_project(tmp_path, "Coding", "#16A34A", 1)

    set_shortcut_override(tmp_path, "research", "terminal", False, None)

    assert _overrides(tmp_path, "research") == {"terminal": {"is_pinned": False}}
    assert _overrides(tmp_path, "coding") == {}


def test_pinning_a_shortcut_back_returns_it_to_the_rail(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    set_shortcut_override(tmp_path, "research", "files", False, None)
    set_shortcut_override(tmp_path, "research", "files", True, None)
    assert _overrides(tmp_path, "research") == {}


def test_setting_a_shortcut_to_what_it_already_is_changes_nothing(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    set_shortcut_override(tmp_path, "research", "chat", False, None)
    assert set_shortcut_override(tmp_path, "research", "chat", False, None) == {"chat": {"is_pinned": False}}
    assert _overrides(tmp_path, "research") == {"chat": {"is_pinned": False}}


def test_a_shortcut_mode_flip_is_stored_sparsely(tmp_path: Path) -> None:
    """Only deviations from the defaults are stored: chat's default is new, the
    rest default to focus, so flipping back to a default clears the field."""
    create_project(tmp_path, "Research", "#12B5A5", 4)

    set_shortcut_override(tmp_path, "research", "chat", None, "focus")
    set_shortcut_override(tmp_path, "research", "terminal", None, "new")
    assert _overrides(tmp_path, "research") == {"chat": {"mode": "focus"}, "terminal": {"mode": "new"}}

    set_shortcut_override(tmp_path, "research", "chat", None, "new")
    set_shortcut_override(tmp_path, "research", "terminal", None, "focus")
    assert _overrides(tmp_path, "research") == {}


def test_an_app_shortcut_stores_only_mode(tmp_path: Path) -> None:
    """App pinning IS membership, so an app: key carries mode and refuses a pin."""
    create_project(tmp_path, "Research", "#12B5A5", 4)

    set_shortcut_override(tmp_path, "research", "app:docs", None, "new")
    assert _overrides(tmp_path, "research") == {"app:docs": {"mode": "new"}}

    with pytest.raises(ProjectShortcutError):
        set_shortcut_override(tmp_path, "research", "app:docs", False, None)


def test_pin_and_mode_land_in_one_override_entry(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    set_shortcut_override(tmp_path, "research", "browser", False, "new")
    assert _overrides(tmp_path, "research") == {"browser": {"is_pinned": False, "mode": "new"}}


def test_an_unknown_shortcut_or_mode_is_refused_rather_than_stored(tmp_path: Path) -> None:
    # Storing one would hide nothing while riding every list response forever.
    create_project(tmp_path, "Research", "#12B5A5", 4)
    with pytest.raises(ProjectShortcutError):
        set_shortcut_override(tmp_path, "research", "not-a-shortcut", False, None)
    with pytest.raises(ProjectShortcutError):
        set_shortcut_override(tmp_path, "research", "chat", None, "sometimes")
    assert _overrides(tmp_path, "research") == {}


def test_hand_edited_override_junk_is_ignored_on_read(tmp_path: Path) -> None:
    """The registry is hand-editable, so junk keys, values, and default-restating
    fields must not survive a read."""
    create_project(tmp_path, "Research", "#12B5A5", 4)
    set_shortcut_override(tmp_path, "research", "browser", False, None)
    meta_path = tmp_path / "projects_meta.json"
    meta = json.loads(meta_path.read_text())
    meta["project_by_id"]["research"]["shortcut_overrides"] = {
        "browser": {"is_pinned": False},
        "made-up": {"is_pinned": False},
        "chat": {"mode": "sometimes"},
        # Restating a default stores nothing.
        "terminal": {"is_pinned": True, "mode": "focus"},
        # An app: key's is_pinned is ignored outright.
        "app:docs": {"is_pinned": False},
        "files": "not-an-object",
    }
    meta_path.write_text(json.dumps(meta))

    assert _overrides(tmp_path, "research") == {"browser": {"is_pinned": False}}


def test_a_legacy_unpinned_shortcuts_list_is_read_and_migrated_on_first_write(tmp_path: Path) -> None:
    """A registry from the projects follow-up stored the unpinned set as a list;
    it reads as {name: {is_pinned: false}} and the first write of any override
    rewrites the entry to the new shape and drops the legacy key."""
    create_project(tmp_path, "Research", "#12B5A5", 4)
    meta_path = tmp_path / "projects_meta.json"
    meta = json.loads(meta_path.read_text())
    entry = meta["project_by_id"]["research"]
    del entry["shortcut_overrides"]
    entry["unpinned_shortcuts"] = ["terminal", "made-up"]
    meta_path.write_text(json.dumps(meta))

    assert _overrides(tmp_path, "research") == {"terminal": {"is_pinned": False}}

    set_shortcut_override(tmp_path, "research", "chat", None, "focus")

    stored = json.loads(meta_path.read_text())["project_by_id"]["research"]
    assert "unpinned_shortcuts" not in stored
    assert stored["shortcut_overrides"] == {
        "terminal": {"is_pinned": False},
        "chat": {"mode": "focus"},
    }


def test_update_project_keeps_the_shortcut_overrides(tmp_path: Path) -> None:
    # A settings save rebuilds the registry entry, and it must carry the
    # shortcut state through the same way it carries the member list -- or
    # renaming a project would silently put every shortcut it had unpinned
    # back in its rail.
    create_project(tmp_path, "Research", "#12B5A5", 4)
    set_shortcut_override(tmp_path, "research", "terminal", False, None)

    updated = update_project(tmp_path, "research", "Research Renamed", "#16A34A", 2)

    assert {k: v.model_dump(exclude_none=True) for k, v in updated.shortcut_overrides.items()} == {
        "terminal": {"is_pinned": False}
    }
    assert _overrides(tmp_path, "research") == {"terminal": {"is_pinned": False}}


def test_remove_member_leaves_other_projects_alone(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    create_project(tmp_path, "Coding", "#16A34A", 1)
    add_member(tmp_path, "research", "service:notes")
    add_member(tmp_path, "coding", "terminal:build")

    remove_member(tmp_path, "research", "service:notes")

    assert list_members(tmp_path, "research") == []
    assert list_members(tmp_path, "coding") == ["terminal:build"]
    assert projects_showing(tmp_path, "service:notes") == []


def test_remove_member_a_project_does_not_show_is_a_noop(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    add_member(tmp_path, "research", "service:notes")
    remove_member(tmp_path, DEFAULT_PROJECT_ID, "service:notes")
    assert projects_showing(tmp_path, "service:notes") == ["research"]


def test_removing_from_one_project_leaves_the_others_showing_it(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    create_project(tmp_path, "Coding", "#16A34A", 1)
    add_member(tmp_path, "research", "service:notes")
    add_member(tmp_path, "coding", "service:notes")

    remove_member(tmp_path, "research", "service:notes")

    assert projects_showing(tmp_path, "service:notes") == ["coding"]


def test_member_calls_reject_unknown_projects(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotFoundError):
        add_member(tmp_path, "ghost", "service:notes")
    with pytest.raises(ProjectNotFoundError):
        remove_member(tmp_path, "ghost", "service:notes")
    with pytest.raises(ProjectNotFoundError):
        list_members(tmp_path, "ghost")


def test_all_members_maps_every_ref_to_its_project(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    create_project(tmp_path, "Coding", "#16A34A", 1)
    add_member(tmp_path, "research", "service:notes")
    add_member(tmp_path, "research", "service:browser?session=2")
    add_member(tmp_path, "coding", "terminal:build")

    assert all_members(tmp_path) == {
        "service:notes": ["research"],
        "service:browser?session=2": ["research"],
        "terminal:build": ["coding"],
    }


def test_members_survive_a_registry_written_without_them(tmp_path: Path) -> None:
    (tmp_path / "projects_meta.json").write_text(
        '{"project_by_id": {"research": {"name": "Research", "color": "#12B5A5", "glyph": 4}}, '
        '"last_active_id": "research"}'
    )
    assert list_members(tmp_path, "research") == []
    add_member(tmp_path, "research", "service:notes")
    assert list_members(tmp_path, "research") == ["service:notes"]


def test_content_contains_panel_detects_membership() -> None:
    content = _content_with_panels("chat-a", "chat-b")
    assert content_contains_panel(content, "chat-a")
    assert not content_contains_panel(content, "chat-missing")
    assert not content_contains_panel({}, "chat-a")


def test_strip_panel_removes_it_from_panels_params_and_group() -> None:
    stripped = strip_panel_from_content(_content_with_panels("chat-a", "chat-b"), "chat-a")
    assert stripped is not None
    assert set(stripped["dockview"]["panels"]) == {"chat-b"}
    assert set(stripped["panelParams"]) == {"chat-b"}
    leaf = stripped["dockview"]["grid"]["root"]["data"][0]
    assert leaf["data"]["views"] == ["chat-b"]


def test_strip_panel_repoints_the_active_view() -> None:
    stripped = strip_panel_from_content(_content_with_panels("chat-a", "chat-b"), "chat-a")
    assert stripped is not None
    assert stripped["dockview"]["grid"]["root"]["data"][0]["data"]["activeView"] == "chat-b"


def test_strip_last_panel_yields_no_content() -> None:
    assert strip_panel_from_content(_content_with_panels("chat-only"), "chat-only") is None


def test_strip_collapses_a_group_that_empties_out() -> None:
    content = _content_with_panels("chat-a")
    content["dockview"]["grid"]["root"]["data"].append(
        {"type": "leaf", "data": {"id": "group-2", "views": ["chat-b"], "activeView": "chat-b"}}
    )
    content["dockview"]["panels"]["chat-b"] = {"id": "chat-b"}
    stripped = strip_panel_from_content(content, "chat-a")
    assert stripped is not None
    remaining_groups = stripped["dockview"]["grid"]["root"]["data"]
    assert [group["data"]["id"] for group in remaining_groups] == ["group-2"]


def test_remove_panel_from_all_projects_touches_only_holders(tmp_path: Path) -> None:
    create_project(tmp_path, "Coding", "#16A34A", 1)
    create_project(tmp_path, "Emails", "#3B82F6", 6)
    write_project_content(tmp_path, DEFAULT_PROJECT_ID, _content_with_panels("chat-a", "chat-b"))
    write_project_content(tmp_path, "coding", _content_with_panels("chat-a"))
    write_project_content(tmp_path, "emails", _content_with_panels("chat-b"))

    changed = remove_panel_from_all_projects(tmp_path, "chat-a")

    assert sorted(changed) == ["coding", DEFAULT_PROJECT_ID]
    starter_content = read_project_content(tmp_path, DEFAULT_PROJECT_ID)
    assert starter_content is not None
    assert set(starter_content["dockview"]["panels"]) == {"chat-b"}
    assert not project_content_path(tmp_path, "coding").exists()
    emails_content = read_project_content(tmp_path, "emails")
    assert emails_content is not None
    assert set(emails_content["dockview"]["panels"]) == {"chat-b"}


def test_remove_panel_from_all_projects_sweeps_every_device(tmp_path: Path) -> None:
    # A destroyed object must not restore as a dead tab on mobile just because
    # it was destroyed from desktop -- both device files are swept, and a
    # holder on either device alone still reports as changed.
    create_project(tmp_path, "Coding", "#16A34A", 1)
    write_project_content(tmp_path, "coding", _content_with_panels("chat-a", "chat-b"))
    write_project_content(tmp_path, "coding", _content_with_panels("chat-a"), "mobile")
    write_project_content(tmp_path, DEFAULT_PROJECT_ID, _content_with_panels("chat-a", "chat-c"), "mobile")

    changed = remove_panel_from_all_projects(tmp_path, "chat-a")

    assert sorted(changed) == ["coding", DEFAULT_PROJECT_ID]
    desktop_content = read_project_content(tmp_path, "coding")
    assert desktop_content is not None
    assert set(desktop_content["dockview"]["panels"]) == {"chat-b"}
    # The mobile file held only the destroyed panel, so it is removed outright.
    assert not project_content_path(tmp_path, "coding", "mobile").exists()
    starter_mobile = read_project_content(tmp_path, DEFAULT_PROJECT_ID, "mobile")
    assert starter_mobile is not None
    assert set(starter_mobile["dockview"]["panels"]) == {"chat-c"}


def test_remove_panel_from_all_projects_also_drops_membership(tmp_path: Path) -> None:
    # Destroy is the one thing that unfiles an object: it no longer exists, so
    # the project that owned it must stop listing it as backgrounded.
    create_project(tmp_path, "Coding", "#16A34A", 1)
    write_project_content(tmp_path, "coding", _content_with_panels("terminal-build"))
    add_member(tmp_path, "coding", "terminal:build")

    changed = remove_panel_from_all_projects(tmp_path, "terminal-build", "terminal:build")

    assert changed == ["coding"]
    assert list_members(tmp_path, "coding") == []
    assert projects_showing(tmp_path, "terminal:build") == []
    assert not project_content_path(tmp_path, "coding").exists()


def test_remove_panel_from_all_projects_unfiles_a_backgrounded_member(tmp_path: Path) -> None:
    # The project showing it has no panel for it -- the object was backgrounded
    # -- so membership is the only thing to drop, and the project still reports
    # as changed so its client refreshes the sidebar.
    create_project(tmp_path, "Coding", "#16A34A", 1)
    add_member(tmp_path, "coding", "terminal:build")

    changed = remove_panel_from_all_projects(tmp_path, "terminal-build", "terminal:build")

    assert changed == ["coding"]
    assert all_members(tmp_path) == {}


def test_remove_panel_from_all_projects_is_a_noop_when_absent(tmp_path: Path) -> None:
    write_project_content(tmp_path, DEFAULT_PROJECT_ID, _content_with_panels("chat-a"))
    add_member(tmp_path, DEFAULT_PROJECT_ID, "chat:agent-a")
    assert remove_panel_from_all_projects(tmp_path, "chat-nowhere", "chat:nobody") == []
    content = read_project_content(tmp_path, DEFAULT_PROJECT_ID)
    assert content is not None
    assert set(content["dockview"]["panels"]) == {"chat-a"}
    assert list_members(tmp_path, DEFAULT_PROJECT_ID) == ["chat:agent-a"]


def test_remove_panel_from_all_projects_also_strips_everything(tmp_path: Path) -> None:
    # Everything has no registry entry, so the project loop never reaches it --
    # but it keeps a saved arrangement like any project, and a destroyed
    # object left there would restore as a dead tab.
    create_project(tmp_path, "Coding", "#16A34A", 1)
    write_project_content(tmp_path, "coding", _content_with_panels("terminal-1", "chat-a"))
    write_project_content(tmp_path, EVERYTHING_VIEW_ID, _content_with_panels("terminal-1", "chat-a"))

    changed = remove_panel_from_all_projects(tmp_path, "terminal-1")

    assert sorted(changed) == ["coding", EVERYTHING_VIEW_ID]
    coding_content = read_project_content(tmp_path, "coding")
    assert coding_content is not None
    assert set(coding_content["dockview"]["panels"]) == {"chat-a"}
    everything_content = read_project_content(tmp_path, EVERYTHING_VIEW_ID)
    assert everything_content is not None
    assert set(everything_content["dockview"]["panels"]) == {"chat-a"}


def test_remove_last_panel_deletes_everything_content_file(tmp_path: Path) -> None:
    # Emptied out entirely, Everything falls back to the fresh-workspace state
    # exactly as a project does: the file goes rather than storing an empty grid.
    write_project_content(tmp_path, EVERYTHING_VIEW_ID, _content_with_panels("terminal-1"))

    changed = remove_panel_from_all_projects(tmp_path, "terminal-1", "terminal:terminal-1")

    assert changed == [EVERYTHING_VIEW_ID]
    assert not project_content_path(tmp_path, EVERYTHING_VIEW_ID).exists()


def test_remove_panel_with_no_everything_content_skips_it(tmp_path: Path) -> None:
    # A machine whose Everything view has never been saved has no file to
    # strip: no crash, and "everything" is not reported as changed.
    write_project_content(tmp_path, DEFAULT_PROJECT_ID, _content_with_panels("chat-a", "chat-b"))

    changed = remove_panel_from_all_projects(tmp_path, "chat-a")

    assert changed == [DEFAULT_PROJECT_ID]


def test_remove_panel_by_ref_strips_minted_id_panels_everywhere(tmp_path: Path) -> None:
    # A browser (or app) pane's panel id is minted per open, so the same
    # object sits under a different id in each view's file and the destroyer's
    # own panel id matches none of them. The ref each saved panel's params
    # resolve to is the object's one stable name, and is what finds them all
    # -- Everything's copy included.
    ref = "service:browser?session=chrome-1"
    create_project(tmp_path, "Coding", "#16A34A", 1)
    add_member(tmp_path, "coding", ref)
    coding_content = _content_with_panels("iframe-browser-1755000000001", "chat-a")
    coding_content["panelParams"]["iframe-browser-1755000000001"] = {
        "serviceName": "browser",
        "url": "http://browser.workspace.test/?session=chrome-1",
    }
    write_project_content(tmp_path, "coding", coding_content)
    everything_content = _content_with_panels("iframe-browser-1755000000002", "chat-a")
    everything_content["panelParams"]["iframe-browser-1755000000002"] = {
        "serviceName": "browser",
        "url": "http://browser.workspace.test/?session=chrome-1",
    }
    write_project_content(tmp_path, EVERYTHING_VIEW_ID, everything_content)

    changed = remove_panel_from_all_projects(tmp_path, None, ref)

    assert sorted(changed) == ["coding", EVERYTHING_VIEW_ID]
    assert list_members(tmp_path, "coding") == []
    coding_after = read_project_content(tmp_path, "coding")
    assert coding_after is not None
    assert set(coding_after["dockview"]["panels"]) == {"chat-a"}
    everything_after = read_project_content(tmp_path, EVERYTHING_VIEW_ID)
    assert everything_after is not None
    assert set(everything_after["dockview"]["panels"]) == {"chat-a"}


def test_remove_panel_matches_both_the_given_id_and_the_ref(tmp_path: Path) -> None:
    # One view saved the terminal under its deterministic id, another under a
    # minted ``iframe-terminal-<ts>`` id (the pre-allocation path): the given
    # panel id catches the first and the ref-resolution catches the second,
    # without double-stripping a panel that matches both.
    ref = "terminal:terminal-1"
    write_project_content(tmp_path, DEFAULT_PROJECT_ID, _content_with_panels("terminal-session-terminal-1", "chat-a"))
    everything_content = _content_with_panels("iframe-terminal-1755000000003", "chat-a")
    everything_content["panelParams"]["iframe-terminal-1755000000003"] = {"terminalSessionName": "terminal-1"}
    write_project_content(tmp_path, EVERYTHING_VIEW_ID, everything_content)

    changed = remove_panel_from_all_projects(tmp_path, "terminal-session-terminal-1", ref)

    assert sorted(changed) == [EVERYTHING_VIEW_ID, DEFAULT_PROJECT_ID]
    default_after = read_project_content(tmp_path, DEFAULT_PROJECT_ID)
    assert default_after is not None
    assert set(default_after["dockview"]["panels"]) == {"chat-a"}
    everything_after = read_project_content(tmp_path, EVERYTHING_VIEW_ID)
    assert everything_after is not None
    assert set(everything_after["dockview"]["panels"]) == {"chat-a"}


def test_member_refs_from_content_covers_every_panel_kind() -> None:
    content = _content_with_panels("chat-agent-7", "terminal-1700000000", "iframe-notes", "iframe-browser", "url-tab")
    content["panelParams"] = {
        "chat-agent-7": {"panelType": "chat", "chatAgentId": "agent-7"},
        "terminal-1700000000": {"panelType": "iframe", "terminalSessionName": "build"},
        "iframe-notes": {"panelType": "iframe", "serviceName": "notes"},
        "iframe-browser": {
            "panelType": "iframe",
            "serviceName": "browser",
            "url": "http://browser.host-0123456789abcdef0123456789abcdef.localhost:8421/?session=2",
        },
        "url-tab": {"panelType": "iframe", "url": "https://example.com/docs"},
    }
    refs = member_refs_from_content(content)
    assert refs[:4] == ["chat:agent-7", "terminal:build", "service:notes", "service:browser?session=2"]
    assert refs[4].startswith("url:")


def test_member_refs_from_content_dedupes_and_tolerates_missing_params() -> None:
    assert member_refs_from_content({}) == []
    content = _content_with_panels("chat-a", "chat-b")
    content["panelParams"] = {
        "chat-a": {"panelType": "chat", "chatAgentId": "agent-1"},
        "chat-b": {"panelType": "chat", "chatAgentId": "agent-1"},
    }
    assert member_refs_from_content(content) == ["chat:agent-1"]


def test_migration_folds_the_desktop_layout_into_one_starter_project(tmp_path: Path) -> None:
    legacy_content = _content_with_panels("chat-agent-7", "iframe-notes")
    legacy_content["panelParams"] = {
        "chat-agent-7": {"panelType": "chat", "chatAgentId": "agent-7"},
        "iframe-notes": {"panelType": "iframe", "serviceName": "notes"},
    }
    _write_legacy_desktop_layout(tmp_path, legacy_content)

    infos = list_projects(tmp_path)

    assert [info.project_id for info in infos] == [DEFAULT_PROJECT_ID]
    assert infos[0].has_content is True
    assert infos[0].members == ("chat:agent-7", "service:notes")
    assert read_project_content(tmp_path, DEFAULT_PROJECT_ID) == legacy_content
    assert get_last_active_id(tmp_path) == DEFAULT_PROJECT_ID
    assert all_members(tmp_path) == {"chat:agent-7": [DEFAULT_PROJECT_ID], "service:notes": [DEFAULT_PROJECT_ID]}


def test_migration_folds_the_old_mobile_layout_into_the_starter_projects_mobile_file(tmp_path: Path) -> None:
    # The retired store kept a separate ``mobile`` layout; it becomes the
    # starter project's mobile arrangement. Members come from desktop alone --
    # membership is shared across devices, so mobile adds none of its own.
    _write_legacy_desktop_layout(tmp_path, _content_with_panels("chat-a"))
    mobile_content = _content_with_panels("chat-m")
    (tmp_path / "layouts" / "mobile.json").write_text(json.dumps(mobile_content, separators=(",", ":")))

    list_projects(tmp_path)

    assert read_project_content(tmp_path, DEFAULT_PROJECT_ID, "mobile") == mobile_content


def test_migration_reaches_a_workspace_still_on_one_implicit_layout(tmp_path: Path) -> None:
    # Two generations back: no named-layout store at all, just the original
    # ``layout.json``, which the migration reads directly as the fallback, so
    # that arrangement still lands in the starter project.
    legacy_content = _content_with_panels("iframe-notes")
    legacy_content["panelParams"] = {"iframe-notes": {"panelType": "iframe", "serviceName": "notes"}}
    (tmp_path / "layout.json").write_text(json.dumps(legacy_content, separators=(",", ":")))

    infos = list_projects(tmp_path)

    assert infos[0].members == ("service:notes",)
    assert read_project_content(tmp_path, DEFAULT_PROJECT_ID) == legacy_content


def test_migration_runs_once(tmp_path: Path) -> None:
    _write_legacy_desktop_layout(tmp_path, _content_with_panels("chat-a"))
    list_projects(tmp_path)

    # Whatever the workspace does next is what the project holds from then on:
    # the named-layout store is still live, and must not keep overwriting it.
    write_project_content(tmp_path, DEFAULT_PROJECT_ID, _content_with_panels("chat-b"))
    remove_member(tmp_path, DEFAULT_PROJECT_ID, member_refs_from_content(_content_with_panels("chat-a"))[0])
    list_projects(tmp_path)

    content = read_project_content(tmp_path, DEFAULT_PROJECT_ID)
    assert content is not None
    assert set(content["dockview"]["panels"]) == {"chat-b"}
    assert list_members(tmp_path, DEFAULT_PROJECT_ID) == []


def test_migration_does_not_clobber_a_starter_project_that_already_has_content(tmp_path: Path) -> None:
    _write_legacy_desktop_layout(tmp_path, _content_with_panels("chat-a"))
    list_projects(tmp_path)
    write_project_content(tmp_path, DEFAULT_PROJECT_ID, _content_with_panels("chat-b"))

    # A registry lost to corruption reseeds, but the project's own content is
    # newer than the layout store's and stays put.
    (tmp_path / "projects_meta.json").write_text("not json{")
    list_projects(tmp_path)

    content = read_project_content(tmp_path, DEFAULT_PROJECT_ID)
    assert content is not None
    assert set(content["dockview"]["panels"]) == {"chat-b"}


def test_migration_skips_an_unreadable_desktop_layout(tmp_path: Path) -> None:
    desktop_path = tmp_path / "layouts" / "desktop.json"
    desktop_path.parent.mkdir(parents=True, exist_ok=True)
    desktop_path.write_text("garbage{")

    infos = list_projects(tmp_path)

    assert [info.project_id for info in infos] == [DEFAULT_PROJECT_ID]
    assert infos[0].has_content is False
    assert infos[0].members == ()


def test_a_workspace_with_no_legacy_layouts_is_not_migrated(tmp_path: Path) -> None:
    infos = list_projects(tmp_path)
    assert infos[0].members == ()
    assert not project_content_path(tmp_path, DEFAULT_PROJECT_ID).exists()
