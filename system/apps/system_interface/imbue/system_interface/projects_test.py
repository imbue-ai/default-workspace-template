from pathlib import Path
from typing import Any

import pytest

from imbue.system_interface.projects import EVERYTHING_PROJECT_ID
from imbue.system_interface.projects import EVERYTHING_PROJECT_NAME
from imbue.system_interface.projects import EverythingProjectDeletionError
from imbue.system_interface.projects import ProjectColorError
from imbue.system_interface.projects import ProjectConflictError
from imbue.system_interface.projects import ProjectGlyphError
from imbue.system_interface.projects import ProjectNameError
from imbue.system_interface.projects import ProjectNotFoundError
from imbue.system_interface.projects import content_contains_panel
from imbue.system_interface.projects import create_project
from imbue.system_interface.projects import delete_project
from imbue.system_interface.projects import get_last_active_id
from imbue.system_interface.projects import list_projects
from imbue.system_interface.projects import project_content_path
from imbue.system_interface.projects import read_project_content
from imbue.system_interface.projects import remove_panel_from_all_projects
from imbue.system_interface.projects import set_last_active_id
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


def test_slugify_project_name_normalizes() -> None:
    assert slugify_project_name("My Fancy Project!") == "my-fancy-project"
    assert slugify_project_name("  Everything  ") == "everything"
    assert slugify_project_name("a_b c") == "a-b-c"


def test_slugify_project_name_rejects_unusable() -> None:
    with pytest.raises(ProjectNameError):
        slugify_project_name("!!!")
    with pytest.raises(ProjectNameError):
        slugify_project_name("   ")


def test_defaults_seed_the_everything_project(tmp_path: Path) -> None:
    infos = list_projects(tmp_path)
    assert [info.project_id for info in infos] == [EVERYTHING_PROJECT_ID]
    everything = infos[0]
    assert everything.name == EVERYTHING_PROJECT_NAME
    assert everything.glyph == 0
    assert everything.has_content is False
    assert get_last_active_id(tmp_path) == EVERYTHING_PROJECT_ID


def test_create_project_round_trips(tmp_path: Path) -> None:
    info = create_project(tmp_path, "Data Pipeline", "#3B82F6", 6)
    assert info.project_id == "data-pipeline"
    assert info.name == "Data Pipeline"
    assert info.color == "#3B82F6"
    assert info.glyph == 6
    assert info.has_content is False

    listed = list_projects(tmp_path)
    assert [listed_info.project_id for listed_info in listed] == [EVERYTHING_PROJECT_ID, "data-pipeline"]
    # A create is immediately followed by a switch in the UI, so it moves the pointer.
    assert get_last_active_id(tmp_path) == "data-pipeline"


def test_create_project_rejects_slug_collision(tmp_path: Path) -> None:
    create_project(tmp_path, "Data Pipeline", "#3B82F6", 6)
    with pytest.raises(ProjectConflictError):
        create_project(tmp_path, "data pipeline", "#16A34A", 1)
    with pytest.raises(ProjectConflictError):
        create_project(tmp_path, EVERYTHING_PROJECT_NAME, "#16A34A", 1)


def test_update_project_keeps_id_and_content(tmp_path: Path) -> None:
    create_project(tmp_path, "Data Pipeline", "#3B82F6", 6)
    content = {"dockview": {"grid": {}}, "panelParams": {"p": {"panelType": "chat"}}}
    write_project_content(tmp_path, "data-pipeline", content)

    updated = update_project(tmp_path, "data-pipeline", "Renamed Entirely", "#EC4899", 9)
    assert updated.project_id == "data-pipeline"
    assert updated.name == "Renamed Entirely"
    assert updated.color == "#EC4899"
    assert updated.glyph == 9
    assert updated.has_content is True
    # The rename did not move the content file, so the tabs stayed put.
    assert read_project_content(tmp_path, "data-pipeline") == content
    assert [info.name for info in list_projects(tmp_path)] == [EVERYTHING_PROJECT_NAME, "Renamed Entirely"]


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
    # Writing content flags the project as non-empty but leaves the pointer alone,
    # so mirroring a new tab into Everything cannot steal the active project.
    write_project_content(tmp_path, EVERYTHING_PROJECT_ID, content)
    assert get_last_active_id(tmp_path) == "research"
    assert [info.has_content for info in list_projects(tmp_path)] == [True, True]


def test_content_access_for_unknown_project_raises(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotFoundError):
        read_project_content(tmp_path, "ghost")
    with pytest.raises(ProjectNotFoundError):
        write_project_content(tmp_path, "ghost", {})


def test_delete_project_returns_everything_fallback(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    write_project_content(tmp_path, "research", {"dockview": {}, "panelParams": {}})
    set_last_active_id(tmp_path, "research")

    fallback_id = delete_project(tmp_path, "research")
    assert fallback_id == EVERYTHING_PROJECT_ID
    assert not project_content_path(tmp_path, "research").exists()
    assert [info.project_id for info in list_projects(tmp_path)] == [EVERYTHING_PROJECT_ID]
    assert get_last_active_id(tmp_path) == EVERYTHING_PROJECT_ID

    with pytest.raises(ProjectNotFoundError):
        delete_project(tmp_path, "research")


def test_delete_everything_project_raises(tmp_path: Path) -> None:
    with pytest.raises(EverythingProjectDeletionError):
        delete_project(tmp_path, EVERYTHING_PROJECT_ID)
    assert [info.project_id for info in list_projects(tmp_path)] == [EVERYTHING_PROJECT_ID]


def test_set_last_active_ignores_unknown_id(tmp_path: Path) -> None:
    set_last_active_id(tmp_path, "ghost")
    assert get_last_active_id(tmp_path) == EVERYTHING_PROJECT_ID


def test_last_active_pointer_moves_and_survives_a_stale_id(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    set_last_active_id(tmp_path, EVERYTHING_PROJECT_ID)
    assert get_last_active_id(tmp_path) == EVERYTHING_PROJECT_ID

    # A registry edited from outside can point at a project that no longer exists.
    (tmp_path / "projects_meta.json").write_text(
        '{"project_by_id": {"everything": {"name": "Everything", "color": "#F0603A", "glyph": 0}}, '
        '"last_active_id": "vanished"}'
    )
    assert get_last_active_id(tmp_path) == EVERYTHING_PROJECT_ID


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
        update_project(tmp_path, EVERYTHING_PROJECT_ID, "  ", "#3B82F6", 0)


def test_corrupt_meta_recovers_to_defaults(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    (tmp_path / "projects_meta.json").write_text("not json{")
    infos = list_projects(tmp_path)
    assert [info.project_id for info in infos] == [EVERYTHING_PROJECT_ID]
    assert get_last_active_id(tmp_path) == EVERYTHING_PROJECT_ID


def test_missing_everything_entry_is_restored(tmp_path: Path) -> None:
    create_project(tmp_path, "Research", "#12B5A5", 4)
    (tmp_path / "projects_meta.json").write_text(
        '{"project_by_id": {"research": {"name": "Research", "color": "#12B5A5", "glyph": 4}}, '
        '"last_active_id": "research"}'
    )
    assert [info.project_id for info in list_projects(tmp_path)] == [EVERYTHING_PROJECT_ID, "research"]
    assert get_last_active_id(tmp_path) == "research"


def test_corrupt_content_reads_as_empty(tmp_path: Path) -> None:
    write_project_content(tmp_path, EVERYTHING_PROJECT_ID, {"ok": True})
    project_content_path(tmp_path, EVERYTHING_PROJECT_ID).write_text("garbage{")
    assert read_project_content(tmp_path, EVERYTHING_PROJECT_ID) is None


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
    write_project_content(tmp_path, EVERYTHING_PROJECT_ID, _content_with_panels("chat-a", "chat-b"))
    write_project_content(tmp_path, "coding", _content_with_panels("chat-a"))
    write_project_content(tmp_path, "emails", _content_with_panels("chat-b"))

    changed = remove_panel_from_all_projects(tmp_path, "chat-a")

    assert sorted(changed) == ["coding", EVERYTHING_PROJECT_ID]
    everything_content = read_project_content(tmp_path, EVERYTHING_PROJECT_ID)
    assert everything_content is not None
    assert set(everything_content["dockview"]["panels"]) == {"chat-b"}
    assert not project_content_path(tmp_path, "coding").exists()
    emails_content = read_project_content(tmp_path, "emails")
    assert emails_content is not None
    assert set(emails_content["dockview"]["panels"]) == {"chat-b"}


def test_remove_panel_from_all_projects_is_a_noop_when_absent(tmp_path: Path) -> None:
    write_project_content(tmp_path, EVERYTHING_PROJECT_ID, _content_with_panels("chat-a"))
    assert remove_panel_from_all_projects(tmp_path, "chat-nowhere") == []
    content = read_project_content(tmp_path, EVERYTHING_PROJECT_ID)
    assert content is not None
    assert set(content["dockview"]["panels"]) == {"chat-a"}
