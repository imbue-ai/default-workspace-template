"""Tests for the pure desktop editor: the shared geometry vectors, and the verbs over desktops and layouts."""

import json
from pathlib import Path
from typing import Any

import pytest
from app_manifest.manifest import ShortcutMode
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from app_manifest.registry import read_registry

from imbue.imbue_common.model_update import to_update
from imbue.system_interface.shell.data_types import Desktop
from imbue.system_interface.shell.data_types import DesktopLayout
from imbue.system_interface.shell.data_types import DesktopShortcut
from imbue.system_interface.shell.data_types import Frame
from imbue.system_interface.shell.data_types import GridCell
from imbue.system_interface.shell.data_types import ShortcutTarget
from imbue.system_interface.shell.data_types import WindowPlacement
from imbue.system_interface.shell.desktop_document import BackdropSize
from imbue.system_interface.shell.desktop_document import FitMetrics
from imbue.system_interface.shell.desktop_document import GridDimensions
from imbue.system_interface.shell.desktop_document import GridMetrics
from imbue.system_interface.shell.desktop_document import cascade_frame
from imbue.system_interface.shell.desktop_document import clamp_frame_into_unit_square
from imbue.system_interface.shell.desktop_document import default_launch_path_id
from imbue.system_interface.shell.desktop_document import effective_placements
from imbue.system_interface.shell.desktop_document import find_window_at
from imbue.system_interface.shell.desktop_document import fit_frame_to_backdrop
from imbue.system_interface.shell.desktop_document import focused_window_id
from imbue.system_interface.shell.desktop_document import frame_for_state
from imbue.system_interface.shell.desktop_document import grid_dimensions
from imbue.system_interface.shell.desktop_document import most_recently_focused_window_of_app
from imbue.system_interface.shell.desktop_document import nearest_free_cell
from imbue.system_interface.shell.desktop_document import next_shortcut_cell
from imbue.system_interface.shell.desktop_document import path_carries_marker
from imbue.system_interface.shell.desktop_document import place_shortcuts
from imbue.system_interface.shell.desktop_document import reading_order_cell
from imbue.system_interface.shell.desktop_document import seed_desktop_shortcuts
from imbue.system_interface.shell.desktop_document import snap_zone_for_release
from imbue.system_interface.shell.desktop_document import unsnap_frame
from imbue.system_interface.shell.desktop_document import with_shortcut
from imbue.system_interface.shell.desktop_document import with_shortcut_moved
from imbue.system_interface.shell.desktop_document import with_window_frame
from imbue.system_interface.shell.desktop_document import with_window_location
from imbue.system_interface.shell.desktop_document import with_window_minimized
from imbue.system_interface.shell.desktop_document import with_window_placed_on_open
from imbue.system_interface.shell.desktop_document import with_window_raised
from imbue.system_interface.shell.desktop_document import with_window_restored
from imbue.system_interface.shell.desktop_document import with_window_state
from imbue.system_interface.shell.desktop_document import without_shortcut
from imbue.system_interface.shell.errors import WindowNotFoundError
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowState
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.shell.testing import desktop_with_windows
from imbue.system_interface.shell.testing import placement_record
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import window_record
from imbue.system_interface.shell.testing import write_registry

_VECTORS_PATH = (
    Path(__file__).resolve().parents[6]
    / "docs"
    / "system"
    / "blueprint"
    / "desktop-interface"
    / "geometry_vectors.json"
)
_VECTORS: dict[str, Any] = json.loads(_VECTORS_PATH.read_text())

_WIN_1 = WindowId("win-0000000000000001")
_WIN_2 = WindowId("win-0000000000000002")
_WIN_3 = WindowId("win-0000000000000003")


def _frame(raw: dict[str, float]) -> Frame:
    return Frame(x=raw["x"], y=raw["y"], width=raw["width"], height=raw["height"])


def _cell(raw: dict[str, int]) -> GridCell:
    return GridCell(column=raw["column"], row=raw["row"])


def _assert_frames_close(actual: Frame, expected: Frame) -> None:
    assert actual.x == pytest.approx(expected.x)
    assert actual.y == pytest.approx(expected.y)
    assert actual.width == pytest.approx(expected.width)
    assert actual.height == pytest.approx(expected.height)


# The shared vectors


@pytest.mark.parametrize("case", _VECTORS["cascade"], ids=lambda case: f"n={case['placed_count']}")
def test_cascade_vectors(case: dict[str, Any]) -> None:
    _assert_frames_close(cascade_frame(case["placed_count"]), _frame(case["expected"]))


@pytest.mark.parametrize("case", _VECTORS["clamp_frame"])
def test_clamp_frame_vectors(case: dict[str, Any]) -> None:
    raw = case["frame"]
    _assert_frames_close(
        clamp_frame_into_unit_square(raw["x"], raw["y"], raw["width"], raw["height"]), _frame(case["expected"])
    )


@pytest.mark.parametrize("state_name", ["SNAPPED_LEFT", "SNAPPED_RIGHT", "MAXIMIZED"])
def test_snap_frame_vectors(state_name: str) -> None:
    placement_frame = Frame(x=0.1, y=0.1, width=0.3, height=0.3)
    assert frame_for_state(placement_frame, WindowState(state_name)) == _frame(_VECTORS["snap_frames"][state_name])
    assert frame_for_state(placement_frame, WindowState.NORMAL) == placement_frame


@pytest.mark.parametrize("case", _VECTORS["fit"], ids=lambda case: case["name"])
def test_fit_vectors(case: dict[str, Any]) -> None:
    metrics = FitMetrics.model_validate(_VECTORS["fit_metrics"])
    fitted = fit_frame_to_backdrop(_frame(case["frame"]), BackdropSize.model_validate(case["backdrop"]), metrics)
    assert fitted.model_dump() == pytest.approx(case["expected"])


@pytest.mark.parametrize("case", _VECTORS["snap_zone"])
def test_snap_zone_vectors(case: dict[str, Any]) -> None:
    zone = snap_zone_for_release(
        case["pointer"]["x"],
        case["pointer"]["y"],
        BackdropSize.model_validate(case["backdrop"]),
        _VECTORS["snap_threshold"],
    )
    assert (zone.value if zone is not None else None) == case["expected"]


@pytest.mark.parametrize("case", _VECTORS["unsnap"], ids=lambda case: case["name"])
def test_unsnap_vectors(case: dict[str, Any]) -> None:
    unsnapped = unsnap_frame(
        _frame(case["kept_frame"]),
        case["pointer"]["x"],
        case["pointer"]["y"],
        case["grab_fraction"],
        case["grab_offset_y"],
    )
    _assert_frames_close(unsnapped, _frame(case["expected"]))


@pytest.mark.parametrize("case", _VECTORS["grid_dimensions"])
def test_grid_dimension_vectors(case: dict[str, Any]) -> None:
    metrics = GridMetrics.model_validate(_VECTORS["grid_metrics"])
    assert grid_dimensions(BackdropSize.model_validate(case["backdrop"]), metrics) == GridDimensions.model_validate(
        case["expected"]
    )


@pytest.mark.parametrize("case", _VECTORS["reading_order"])
def test_reading_order_vectors(case: dict[str, Any]) -> None:
    assert reading_order_cell(case["index"], case["columns"]) == _cell(case["expected"])


@pytest.mark.parametrize("case", _VECTORS["nearest_free_cell"], ids=lambda case: case["name"])
def test_nearest_free_cell_vectors(case: dict[str, Any]) -> None:
    found = nearest_free_cell(
        _cell(case["target"]),
        {_cell(raw) for raw in case["occupied"]},
        GridDimensions.model_validate(case["grid"]),
    )
    assert found == _cell(case["expected"])


@pytest.mark.parametrize("case", _VECTORS["shortcut_placement"], ids=lambda case: case["name"])
def test_shortcut_placement_vectors(case: dict[str, Any]) -> None:
    shortcuts = [
        DesktopShortcut(
            target=ShortcutTarget(app=AppName(f"app{index}"), launch=LaunchPathId("new")),
            mode=ShortcutMode.FOCUS,
            cell=_cell(raw),
        )
        for index, raw in enumerate(case["cells"])
    ]
    placed = place_shortcuts(shortcuts, GridDimensions.model_validate(case["grid"]))
    assert [entry.cell for entry in placed] == [_cell(raw) for raw in case["expected"]]
    assert [entry.shortcut for entry in placed] == shortcuts


def test_nearest_free_cell_without_a_grid_searches_the_unbounded_plane() -> None:
    occupied = {GridCell(column=0, row=0), GridCell(column=0, row=1), GridCell(column=1, row=0)}
    assert nearest_free_cell(GridCell(column=0, row=0), occupied, None) == GridCell(column=1, row=1)
    assert nearest_free_cell(GridCell(column=3, row=3), occupied, None) == GridCell(column=3, row=3)


def test_nearest_free_cell_without_a_grid_prefers_a_nearer_cell_outside_the_first_square_with_a_free_one() -> None:
    """With the 7x7 square around the target full but for a corner, the corner (distance 3*sqrt(2)) loses to the
    cells one column past the square's edge on the target's row (distance 4), the lower column winning the tie;
    with the 9x9 square full but for that corner, the corner wins over everything at distance 5 and beyond."""
    target = GridCell(column=10, row=10)
    corner = GridCell(column=13, row=13)
    seven_square = {
        GridCell(column=column, row=row)
        for column in range(7, 14)
        for row in range(7, 14)
        if not (column == 13 and row == 13)
    }
    assert nearest_free_cell(target, seven_square, None) == GridCell(column=6, row=10)
    nine_square = {
        GridCell(column=column, row=row)
        for column in range(6, 15)
        for row in range(6, 15)
        if not (column == 13 and row == 13)
    }
    assert nearest_free_cell(target, nine_square, None) == corner


# Desktops: windows


def test_a_window_is_found_by_app_and_exact_path_and_a_marker_in_a_segment_or_query_value() -> None:
    desktop = desktop_with_windows(
        window_record(_WIN_1, "chat", "/?chat=agent-1"), window_record(_WIN_2, "terminal", "/?session=terminal-3")
    )
    found = find_window_at(desktop, AppName("chat"), WindowPath("/?chat=agent-1"))
    assert found is not None and found.id == _WIN_1
    assert find_window_at(desktop, AppName("chat"), WindowPath("/?chat=agent-2")) is None
    assert path_carries_marker(WindowPath("/?chat=agent-1"), "agent-1")
    assert path_carries_marker(WindowPath("/agent-1.sub.1"), "agent-1") is False
    assert path_carries_marker(WindowPath("/agent-1"), "agent-1")
    assert path_carries_marker(WindowPath("/x/agent-1/y?other=z"), "agent-1")
    assert path_carries_marker(WindowPath("/?chat=agent-10"), "agent-1") is False


def test_a_location_report_replaces_the_path_and_title_and_ends_settling_only_when_something_changes() -> None:
    desktop = desktop_with_windows(window_record(_WIN_1, "chat", "/new?message=hi", is_settling=True))
    landed = with_window_location(desktop, _WIN_1, WindowPath("/?chat=agent-9"), WindowTitle("Plan"))
    assert landed.windows[0].path == "/?chat=agent-9"
    assert landed.windows[0].title == "Plan" and landed.windows[0].is_settling is False
    assert with_window_location(landed, _WIN_1, WindowPath("/?chat=agent-9"), WindowTitle("Plan")) is landed
    with pytest.raises(WindowNotFoundError):
        with_window_location(desktop, _WIN_2, WindowPath("/"), WindowTitle(""))


# Desktops: shortcuts


def _shortcut(
    app: str, launch: str, column: int, row: int, mode: ShortcutMode = ShortcutMode.FOCUS
) -> DesktopShortcut:
    return DesktopShortcut(
        target=ShortcutTarget(app=AppName(app), launch=LaunchPathId(launch)),
        mode=mode,
        cell=GridCell(column=column, row=row),
    )


def _desktop_with_shortcuts(*shortcuts: DesktopShortcut) -> Desktop:
    empty = desktop_with_windows()
    return empty.model_copy_update(to_update(empty.field_ref().shortcuts, shortcuts))


def test_setting_a_shortcut_replaces_the_same_target_in_place_and_appends_a_new_one() -> None:
    desktop = _desktop_with_shortcuts(_shortcut("chat", "new", 0, 0), _shortcut("files", "new", 0, 1))
    flipped = with_shortcut(desktop, _shortcut("chat", "new", 0, 0, ShortcutMode.NEW))
    assert [shortcut.target.app for shortcut in flipped.shortcuts] == ["chat", "files"]
    assert flipped.shortcuts[0].mode is ShortcutMode.NEW
    added = with_shortcut(flipped, _shortcut("terminal", "new", 0, 2))
    assert [shortcut.target.app for shortcut in added.shortcuts] == ["chat", "files", "terminal"]
    assert next_shortcut_cell(added) == GridCell(column=0, row=3)
    removed = without_shortcut(added, AppName("files"), LaunchPathId("new"))
    assert [shortcut.target.app for shortcut in removed.shortcuts] == ["chat", "terminal"]


def test_moving_a_shortcut_onto_an_occupied_cell_displaces_the_occupant_to_the_nearest_free_cell() -> None:
    desktop = _desktop_with_shortcuts(_shortcut("chat", "new", 0, 0), _shortcut("files", "new", 0, 1))
    moved = with_shortcut_moved(desktop, AppName("files"), LaunchPathId("new"), GridCell(column=0, row=0))
    assert {shortcut.target.app: shortcut.cell for shortcut in moved.shortcuts} == {
        "files": GridCell(column=0, row=0),
        "chat": GridCell(column=0, row=1),
    }
    assert with_shortcut_moved(desktop, AppName("nope"), LaunchPathId("new"), GridCell(column=3, row=3)) is desktop


def test_a_new_desktop_is_seeded_from_every_non_internal_default_shortcut_in_one_column(tmp_path: Path) -> None:
    rows = read_registry(
        write_registry(
            tmp_path / "apps.toml",
            registry_row_toml(
                "chat",
                "http://localhost:1",
                launch_paths=[("new", "New Chat", "/new")],
                default_shortcut=("new", "new"),
            ),
            registry_row_toml("hidden", "http://localhost:2", is_internal=True, default_shortcut=("open", "focus")),
            registry_row_toml("plain", "http://localhost:3"),
            registry_row_toml("files", "http://localhost:4", default_shortcut=("open", "focus")),
            # A default shortcut naming a launch path the row does not declare seeds nothing.
            registry_row_toml("odd", "http://localhost:5", default_shortcut=("make", "focus")),
        )
    )
    seeded = seed_desktop_shortcuts(rows)
    assert [(str(shortcut.target.app), str(shortcut.target.launch), shortcut.mode.value) for shortcut in seeded] == [
        ("chat", "new", "new"),
        ("files", "open", "focus"),
    ]
    assert [shortcut.cell for shortcut in seeded] == [GridCell(column=0, row=0), GridCell(column=0, row=1)]
    # The same rule answers what a bare ``open`` op runs: the declared launch, or nothing for a launch path the
    # app does not offer and for an app with no default shortcut.
    assert {str(row.name): default_launch_path_id(row) for row in rows} == {
        "chat": "new",
        "hidden": "open",
        "plain": None,
        "files": "open",
        "odd": None,
    }


# Layouts


def _layout(*placements: WindowPlacement) -> DesktopLayout:
    return DesktopLayout(version=1, updated_at=None, placements=placements)


def test_windows_without_a_placement_read_as_minimized_at_the_bottom_and_stale_placements_are_dropped() -> None:
    desktop: Desktop = desktop_with_windows(
        window_record(_WIN_1, "chat", "/a"), window_record(_WIN_2, "files", "/b"), window_record(_WIN_3, "chat", "/c")
    )
    layout = _layout(
        placement_record(_WIN_3),
        placement_record(WindowId("win-00000000000000ff")),
        placement_record(_WIN_1, is_minimized=True),
    )
    effective = effective_placements(layout, desktop)
    assert [placement.window_id for placement in effective] == [_WIN_2, _WIN_3, _WIN_1]
    assert effective[0].is_minimized is True and effective[0].frame == cascade_frame(2)
    assert focused_window_id(effective) == _WIN_3
    recent = most_recently_focused_window_of_app(layout, desktop, AppName("chat"))
    assert recent is not None and recent.id == _WIN_1
    assert most_recently_focused_window_of_app(layout, desktop, AppName("browser")) is None


def test_the_verbs_edit_one_placement_and_the_stack() -> None:
    layout = _layout(placement_record(_WIN_1), placement_record(_WIN_2))
    opened = with_window_placed_on_open(layout, _WIN_3)
    assert [placement.window_id for placement in opened.placements] == [_WIN_1, _WIN_2, _WIN_3]
    assert opened.placements[-1].frame == cascade_frame(2) and opened.placements[-1].is_minimized is False
    raised = with_window_raised(opened, _WIN_1)
    assert [placement.window_id for placement in raised.placements] == [_WIN_2, _WIN_3, _WIN_1]
    minimized = with_window_minimized(raised, _WIN_1)
    assert [placement.window_id for placement in minimized.placements] == [_WIN_2, _WIN_3, _WIN_1]
    assert minimized.placements[-1].is_minimized is True
    assert focused_window_id(minimized.placements) == _WIN_3
    maximized = with_window_state(minimized, _WIN_2, WindowState.MAXIMIZED)
    assert maximized.placements[-1].window_id == _WIN_2 and maximized.placements[-1].state is WindowState.MAXIMIZED
    assert maximized.placements[-1].frame == cascade_frame(0)
    restored = with_window_restored(maximized, _WIN_1)
    assert restored.placements[-1].window_id == _WIN_1
    assert restored.placements[-1].is_minimized is False and restored.placements[-1].state is WindowState.NORMAL
    framed = with_window_frame(restored, _WIN_3, Frame(x=0.2, y=0.2, width=0.3, height=0.3))
    assert framed.placements[-1].window_id == _WIN_3 and framed.placements[-1].frame.x == 0.2
    # A window with no placement yet gets its default before the verb applies.
    absent = with_window_raised(_layout(), _WIN_1)
    assert absent.placements == (placement_record(_WIN_1),)
