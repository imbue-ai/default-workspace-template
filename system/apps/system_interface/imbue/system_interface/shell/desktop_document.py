"""The pure editor over the desktop model (desktop-interface plan section 5.3, contracts.md section 10).

Pure functions over the frozen ``Desktop``, ``Window``, ``DesktopLayout``, and ``WindowPlacement`` records that
both the routes and the agent ops use: open, close, focus, minimize, restore, maximize, snap, place, and
the shortcut edits, plus the geometry rules (cascade, fit, snap zones, un-snap, the grid, the nearest free
cell, reading order, and the render-time placement of shortcuts). Every rule the frontend also applies is
written once here and once in TypeScript against the same constants and the shared vectors in
``docs/system/blueprint/desktop-interface/geometry_vectors.json``, which ``desktop_document_test.py`` runs.
"""

import math
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime
from typing import Final
from typing import assert_never
from urllib.parse import parse_qsl
from urllib.parse import urlsplit

from app_manifest.manifest import LocationScope
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from app_manifest.registry import RegistryRow
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import AppPin
from imbue.system_interface.shell.data_types import Desktop
from imbue.system_interface.shell.data_types import DesktopChangeOutcome
from imbue.system_interface.shell.data_types import DesktopLayout
from imbue.system_interface.shell.data_types import DesktopShortcut
from imbue.system_interface.shell.data_types import Frame
from imbue.system_interface.shell.data_types import GridCell
from imbue.system_interface.shell.data_types import ShortcutTarget
from imbue.system_interface.shell.data_types import Window
from imbue.system_interface.shell.data_types import WindowPlacement
from imbue.system_interface.shell.data_types import effective_launch_paths
from imbue.system_interface.shell.errors import GridSearchExhaustedError
from imbue.system_interface.shell.errors import WindowNotFoundError
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowState
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.shell.primitives import mint_window_id

# The cascade rule (fixed, in fractions): window ``n`` of a client's desktop steps from the origin, cycling.
CASCADE_ORIGIN_X: Final[float] = 0.05
CASCADE_ORIGIN_Y: Final[float] = 0.06
CASCADE_STEP_X: Final[float] = 0.03
CASCADE_STEP_Y: Final[float] = 0.04
CASCADE_WIDTH: Final[float] = 0.6
CASCADE_HEIGHT: Final[float] = 0.7
CASCADE_CYCLE: Final[int] = 6

# The snap frames (fixed).
SNAPPED_LEFT_FRAME: Final[Frame] = Frame(x=0.0, y=0.0, width=0.5, height=1.0)
SNAPPED_RIGHT_FRAME: Final[Frame] = Frame(x=0.5, y=0.0, width=0.5, height=1.0)
MAXIMIZED_FRAME: Final[Frame] = Frame(x=0.0, y=0.0, width=1.0, height=1.0)

# The shell seeds a new desktop's shortcuts and places a shortcut added by an agent without knowing any
# backdrop, so it lays them out in reading order over a grid this many columns wide: one column down the
# left edge, which every grid a backdrop can hold contains.
SEED_GRID_COLUMNS: Final[int] = 1

DESKTOPS_FILE_VERSION: Final[int] = 1
PLACEMENTS_FILE_VERSION: Final[int] = 1


class BackdropSize(FrozenModel):
    """The backdrop in pixels: the viewport less the taskbar."""

    width: float = Field(description="Pixels")
    height: float = Field(description="Pixels")


class PixelRect(FrozenModel):
    """A rendered window rectangle in backdrop pixels."""

    x: float = Field(description="Pixels from the backdrop's left")
    y: float = Field(description="Pixels from the backdrop's top")
    width: float = Field(description="Pixels")
    height: float = Field(description="Pixels")


class FitMetrics(FrozenModel):
    """The theme metrics the fit rule reads (desktop contracts.md section 11)."""

    window_min_width: float = Field(description="Pixels a window is never rendered narrower than")
    window_min_height: float = Field(description="Pixels a window is never rendered shorter than")
    title_min_visible: float = Field(description="Pixels of the title bar that stay inside horizontally")
    title_bar_height: float = Field(description="Pixels; the whole title bar stays inside vertically")


class GridMetrics(FrozenModel):
    """The theme metrics the grid rule reads."""

    cell_width: float = Field(description="Pixels")
    cell_height: float = Field(description="Pixels")
    inset: float = Field(description="Pixels from the backdrop's top-left to the grid origin")


class GridDimensions(FrozenModel):
    """How many cells the backdrop holds."""

    columns: int = Field(ge=1, description="At least one")
    rows: int = Field(ge=1, description="At least one")


class PlacedShortcut(FrozenModel):
    """A shortcut with the cell it is drawn in, which may differ from its stored cell."""

    shortcut: DesktopShortcut = Field(description="The shortcut as stored")
    cell: GridCell = Field(description="The cell it draws in on this grid")


# Geometry: frames


@pure
def clamp_frame_into_unit_square(x: float, y: float, width: float, height: float) -> Frame:
    """The frame shifted (and, past the square's size, shrunk) so that it lies wholly inside the unit square."""
    clamped_width = min(max(width, 0.0), 1.0)
    clamped_height = min(max(height, 0.0), 1.0)
    clamped_x = min(max(x, 0.0), 1.0 - clamped_width)
    clamped_y = min(max(y, 0.0), 1.0 - clamped_height)
    return Frame(x=clamped_x, y=clamped_y, width=clamped_width, height=clamped_height)


@pure
def cascade_frame(placed_count: int) -> Frame:
    """The frame the ``placed_count``-th window a client places on a desktop opens at (zero-based, cycling)."""
    step = placed_count % CASCADE_CYCLE
    return clamp_frame_into_unit_square(
        CASCADE_ORIGIN_X + CASCADE_STEP_X * step,
        CASCADE_ORIGIN_Y + CASCADE_STEP_Y * step,
        CASCADE_WIDTH,
        CASCADE_HEIGHT,
    )


@pure
def frame_for_state(placement_frame: Frame, state: WindowState) -> Frame:
    """The frame a window renders at: its own when normal, the fixed half or whole otherwise."""
    match state:
        case WindowState.NORMAL:
            return placement_frame
        case WindowState.SNAPPED_LEFT:
            return SNAPPED_LEFT_FRAME
        case WindowState.SNAPPED_RIGHT:
            return SNAPPED_RIGHT_FRAME
        case WindowState.MAXIMIZED:
            return MAXIMIZED_FRAME
        case _ as unreachable:
            assert_never(unreachable)


@pure
def fit_frame_to_backdrop(frame: Frame, backdrop: BackdropSize, metrics: FitMetrics) -> PixelRect:
    """The fit rule (render only): fractions to pixels, the minimum size enforced, the title bar nudged into view."""
    width = max(frame.width * backdrop.width, metrics.window_min_width)
    height = max(frame.height * backdrop.height, metrics.window_min_height)
    scaled_x = frame.x * backdrop.width
    scaled_y = frame.y * backdrop.height
    # Horizontally at least the minimum visible title width stays inside, on either side.
    visible = min(metrics.title_min_visible, width)
    x = min(max(scaled_x, visible - width), backdrop.width - visible)
    # Vertically the whole title bar stays inside, and the top edge is never above the backdrop's.
    y = max(min(scaled_y, backdrop.height - metrics.title_bar_height), 0.0)
    return PixelRect(x=x, y=y, width=width, height=height)


@pure
def snap_zone_for_release(
    pointer_x: float, pointer_y: float, backdrop: BackdropSize, threshold: float
) -> WindowState | None:
    """The state a drag released at the pointer snaps to, or None outside every zone; the top edge wins a corner."""
    if pointer_y <= threshold:
        return WindowState.MAXIMIZED
    if pointer_x <= threshold:
        return WindowState.SNAPPED_LEFT
    if pointer_x >= backdrop.width - threshold:
        return WindowState.SNAPPED_RIGHT
    return None


@pure
def unsnap_frame(
    kept_frame: Frame, pointer_x: float, pointer_y: float, grab_fraction: float, grab_offset_y: float
) -> Frame:
    """The un-snap rule: the kept frame's size, hung so the pointer (in fractions of the backdrop) sits at
    ``grab_fraction`` across the title bar and ``grab_offset_y`` below the window's top, then clamped."""
    return clamp_frame_into_unit_square(
        pointer_x - grab_fraction * kept_frame.width,
        pointer_y - grab_offset_y,
        kept_frame.width,
        kept_frame.height,
    )


# Geometry: the grid


@pure
def grid_dimensions(backdrop: BackdropSize, metrics: GridMetrics) -> GridDimensions:
    return GridDimensions(
        columns=max(1, math.floor((backdrop.width - metrics.inset) / metrics.cell_width)),
        rows=max(1, math.floor((backdrop.height - metrics.inset) / metrics.cell_height)),
    )


@pure
def reading_order_cell(index: int, columns: int) -> GridCell:
    return GridCell(column=index % columns, row=index // columns)


@pure
def _clamp_cell_into_grid(cell: GridCell, dimensions: GridDimensions) -> GridCell:
    return GridCell(column=min(cell.column, dimensions.columns - 1), row=min(cell.row, dimensions.rows - 1))


@pure
def _is_cell_inside_grid(cell: GridCell, dimensions: GridDimensions) -> bool:
    return cell.column < dimensions.columns and cell.row < dimensions.rows


@pure
def _cell_distance(first: GridCell, second: GridCell) -> float:
    return math.hypot(first.column - second.column, first.row - second.row)


@pure
def _nearness_key(cell: GridCell, target: GridCell) -> tuple[float, int, int]:
    """Sorts cells by Euclidean distance from the target, ties by lower column then lower row."""
    return (_cell_distance(cell, target), cell.column, cell.row)


@pure
def _nearest_of(candidates: Sequence[GridCell], target: GridCell) -> GridCell | None:
    """The candidate at the least Euclidean distance from the target, ties by lower column then lower row."""
    return min(candidates, key=lambda cell: _nearness_key(cell, target), default=None)


@pure
def nearest_free_cell(
    target: GridCell, occupied: AbstractSet[GridCell], dimensions: GridDimensions | None
) -> GridCell:
    """The nearest free cell to the target clamped into the grid (ties by lower column then lower row).

    With dimensions the search is over the grid, and a grid with no free cell answers the clamped target
    itself; without dimensions (the shell placing a shortcut with no backdrop in sight) the grid is
    unbounded below and to the right, so a free cell always exists within a few rings of the target.
    """
    if dimensions is not None:
        clamped = _clamp_cell_into_grid(target, dimensions)
        free = [
            GridCell(column=column, row=row)
            for column in range(dimensions.columns)
            for row in range(dimensions.rows)
            if GridCell(column=column, row=row) not in occupied
        ]
        nearest = _nearest_of(free, clamped)
        return nearest if nearest is not None else clamped
    if target not in occupied:
        return target
    # The search widens a square around the target one ring at a time. A square one wider than the number of
    # occupied cells cannot be fully occupied, so a candidate turns up within that many rings; and since every
    # cell outside a square of radius R lies at least R + 1 away, the best candidate is final once the square
    # has grown past its distance, which takes at most sqrt(2) times as many rings again.
    best: GridCell | None = None
    for radius in range(1, 2 * (len(occupied) + 2)):
        square = [
            GridCell(column=column, row=row)
            for column in range(max(0, target.column - radius), target.column + radius + 1)
            for row in range(max(0, target.row - radius), target.row + radius + 1)
            if GridCell(column=column, row=row) not in occupied
        ]
        nearest = _nearest_of(square, target)
        if nearest is not None and (best is None or _nearness_key(nearest, target) < _nearness_key(best, target)):
            best = nearest
        if best is not None and radius + 1 > _cell_distance(best, target):
            return best
    raise GridSearchExhaustedError("the unbounded grid always holds a free cell")


@pure
def _first_free_cell_in_reading_order(occupied: AbstractSet[GridCell], columns: int) -> GridCell:
    index = 0
    while reading_order_cell(index, columns) in occupied:
        index += 1
    return reading_order_cell(index, columns)


@pure
def place_shortcuts(shortcuts: Sequence[DesktopShortcut], dimensions: GridDimensions) -> list[PlacedShortcut]:
    """The render-time placement: every shortcut whose stored cell is inside the grid and unclaimed takes it,
    in shortcut order; every other shortcut takes the nearest free cell to its clamped stored cell, in order."""
    claimed: set[GridCell] = set()
    placed_by_index: dict[int, GridCell] = {}
    for index, shortcut in enumerate(shortcuts):
        if _is_cell_inside_grid(shortcut.cell, dimensions) and shortcut.cell not in claimed:
            claimed.add(shortcut.cell)
            placed_by_index[index] = shortcut.cell
    for index, shortcut in enumerate(shortcuts):
        if index in placed_by_index:
            continue
        cell = nearest_free_cell(shortcut.cell, claimed, dimensions)
        claimed.add(cell)
        placed_by_index[index] = cell
    return [PlacedShortcut(shortcut=shortcut, cell=placed_by_index[index]) for index, shortcut in enumerate(shortcuts)]


# Desktops: windows


@pure
def find_window(desktop: Desktop, window_id: WindowId) -> Window | None:
    return next((window for window in desktop.windows if window.id == window_id), None)


@pure
def require_window(desktop: Desktop, window_id: WindowId) -> Window:
    window = find_window(desktop, window_id)
    if window is None:
        raise WindowNotFoundError(str(window_id))
    return window


@pure
def find_window_at(desktop: Desktop, app: AppName, path: WindowPath) -> Window | None:
    """The window of ``app`` at exactly ``path`` on the desktop, in opening order, or None."""
    return next((window for window in desktop.windows if window.app == app and window.path == path), None)


@pure
def with_window_opened(desktop: Desktop, window: Window) -> Desktop:
    return desktop.model_copy_update(to_update(desktop.field_ref().windows, (*desktop.windows, window)))


@pure
def without_window(desktop: Desktop, window_id: WindowId) -> Desktop:
    return desktop.model_copy_update(
        to_update(desktop.field_ref().windows, tuple(window for window in desktop.windows if window.id != window_id))
    )


@pure
def with_window_location(desktop: Desktop, window_id: WindowId, path: WindowPath, title: WindowTitle) -> Desktop:
    """The desktop with the window's path and title replaced and its settling over; the same object when nothing changes."""
    window = require_window(desktop, window_id)
    if window.path == path and window.title == title and not window.is_settling:
        return desktop
    updated = window.model_copy_update(
        to_update(window.field_ref().path, path),
        to_update(window.field_ref().title, title),
        to_update(window.field_ref().is_settling, False),
    )
    return desktop.model_copy_update(
        to_update(
            desktop.field_ref().windows,
            tuple(updated if candidate.id == window_id else candidate for candidate in desktop.windows),
        )
    )


# Desktops: pinned windows (pinned-taskbar-entries plan section 3.2)


@pure
def pinned_apps(rows: Sequence[RegistryRow]) -> tuple[AppPin, ...]:
    """The pinned apps: every registered, non-internal app whose row carries a pin, in registry order."""
    return tuple(AppPin(app=row.name, pin=row.pin) for row in rows if row.pin is not None and not row.internal)


def pinned_window(app_pin: AppPin, now: datetime) -> Window:
    """The record an ensure creates: the pin's home path, an empty title, settled, pinned, with the pin's scope."""
    return Window(
        id=mint_window_id(),
        app=app_pin.app,
        path=WindowPath(str(app_pin.pin.path)),
        title=WindowTitle(""),
        opened_at=now,
        is_settling=False,
        is_pinned=True,
        scope=app_pin.pin.scope,
    )


@pure
def _as_pinned(window: Window, app_pin: AppPin) -> Window:
    """The window adopted as the app's pinned window: marked, given the pin's scope, and, when independent, its
    shared title cleared (each client keeps its own from then on)."""
    scope = app_pin.pin.scope
    title = WindowTitle("") if scope is LocationScope.INDEPENDENT else window.title
    if window.is_pinned and window.scope is scope and window.title == title:
        return window
    return window.model_copy_update(
        to_update(window.field_ref().is_pinned, True),
        to_update(window.field_ref().scope, scope),
        to_update(window.field_ref().title, title),
    )


@pure
def _with_pinned_window_ensured(desktop: Desktop, app_pin: AppPin, now: datetime) -> Desktop:
    """The desktop holding the app's pinned window: the one already marked, else the earliest-opened window of the
    app at the home path adopted, else a new one."""
    home_path = WindowPath(str(app_pin.pin.path))
    marked = next((window for window in desktop.windows if window.app == app_pin.app and window.is_pinned), None)
    adoptable = marked if marked is not None else find_window_at(desktop, app_pin.app, home_path)
    if adoptable is None:
        return with_window_opened(desktop, pinned_window(app_pin, now))
    adopted = _as_pinned(adoptable, app_pin)
    if adopted is adoptable:
        return desktop
    return desktop.model_copy_update(
        to_update(
            desktop.field_ref().windows,
            tuple(adopted if candidate.id == adoptable.id else candidate for candidate in desktop.windows),
        )
    )


def with_pinned_windows_ensured(desktop: Desktop, pins: Sequence[AppPin], now: datetime) -> DesktopChangeOutcome:
    """The desktop holding exactly one pinned window per pinned app, adopting or creating as needed, and whether that
    changed it. Not pure: a window the desktop lacks is minted here."""
    ensured = desktop
    for app_pin in pins:
        ensured = _with_pinned_window_ensured(ensured, app_pin, now)
    return DesktopChangeOutcome(desktop=ensured, is_written=ensured is not desktop)


@pure
def without_pin_marks(desktop: Desktop, pinned_app_names: AbstractSet[AppName]) -> Desktop:
    """The desktop with every pinned window of an app no longer pinned turned back into an ordinary window; the same
    object when none is."""
    if not any(window.is_pinned and window.app not in pinned_app_names for window in desktop.windows):
        return desktop
    return desktop.model_copy_update(
        to_update(
            desktop.field_ref().windows,
            tuple(
                window.model_copy_update(to_update(window.field_ref().is_pinned, False))
                if window.is_pinned and window.app not in pinned_app_names
                else window
                for window in desktop.windows
            ),
        )
    )


@pure
def path_carries_marker(path: WindowPath, marker: str) -> bool:
    """Whether a path holds ``marker`` as one of its segments or as a query parameter's value (what ``self`` matches)."""
    split = urlsplit(str(path))
    if marker in split.path.split("/"):
        return True
    return any(value == marker for _name, value in parse_qsl(split.query, keep_blank_values=True))


# Desktops: shortcuts


@pure
def _is_same_target(shortcut: DesktopShortcut, app: AppName, launch: LaunchPathId) -> bool:
    return shortcut.target.app == app and shortcut.target.launch == launch


@pure
def _find_shortcut(desktop: Desktop, app: AppName, launch: LaunchPathId) -> DesktopShortcut | None:
    return next((shortcut for shortcut in desktop.shortcuts if _is_same_target(shortcut, app, launch)), None)


@pure
def _occupied_cells(shortcuts: Sequence[DesktopShortcut]) -> set[GridCell]:
    return {shortcut.cell for shortcut in shortcuts}


@pure
def with_shortcut(desktop: Desktop, shortcut: DesktopShortcut) -> Desktop:
    """The desktop with the shortcut, replacing the entry for the same (app, launch) in place or appending it."""
    existing = _find_shortcut(desktop, shortcut.target.app, shortcut.target.launch)
    if existing is None:
        shortcuts = (*desktop.shortcuts, shortcut)
    else:
        shortcuts = tuple(shortcut if candidate is existing else candidate for candidate in desktop.shortcuts)
    return desktop.model_copy_update(to_update(desktop.field_ref().shortcuts, shortcuts))


@pure
def without_shortcut(desktop: Desktop, app: AppName, launch: LaunchPathId) -> Desktop:
    return desktop.model_copy_update(
        to_update(
            desktop.field_ref().shortcuts,
            tuple(shortcut for shortcut in desktop.shortcuts if not _is_same_target(shortcut, app, launch)),
        )
    )


@pure
def with_shortcut_moved(desktop: Desktop, app: AppName, launch: LaunchPathId, cell: GridCell) -> Desktop:
    """The desktop with the shortcut in ``cell``; a shortcut already there moves to the nearest free cell."""
    moved = _find_shortcut(desktop, app, launch)
    if moved is None:
        return desktop
    occupant = next(
        (shortcut for shortcut in desktop.shortcuts if shortcut.cell == cell and shortcut is not moved), None
    )
    others = tuple(shortcut for shortcut in desktop.shortcuts if shortcut is not moved and shortcut is not occupant)
    displaced_cell = nearest_free_cell(cell, _occupied_cells(others) | {cell}, None) if occupant is not None else None
    shortcuts: list[DesktopShortcut] = []
    for shortcut in desktop.shortcuts:
        if shortcut is moved:
            shortcuts.append(shortcut.model_copy_update(to_update(shortcut.field_ref().cell, cell)))
        elif shortcut is occupant and displaced_cell is not None:
            shortcuts.append(shortcut.model_copy_update(to_update(shortcut.field_ref().cell, displaced_cell)))
        else:
            shortcuts.append(shortcut)
    return desktop.model_copy_update(to_update(desktop.field_ref().shortcuts, tuple(shortcuts)))


@pure
def next_shortcut_cell(desktop: Desktop) -> GridCell:
    """Where a shortcut added with no cell goes: the first free cell in reading order over the seeding grid."""
    return _first_free_cell_in_reading_order(_occupied_cells(desktop.shortcuts), SEED_GRID_COLUMNS)


@pure
def default_launch_path_id(row: RegistryRow) -> LaunchPathId | None:
    """The launch path an app's ``default_shortcut`` names, when the app offers it; None otherwise."""
    if row.default_shortcut is None:
        return None
    offered = {launch_path.id for launch_path in effective_launch_paths(row)}
    declared = row.default_shortcut.launch
    return declared if declared in offered else None


@pure
def seed_desktop_shortcuts(rows: Sequence[RegistryRow]) -> tuple[DesktopShortcut, ...]:
    """A new desktop's shortcuts: every registered, non-internal app's ``default_shortcut`` (``default_launch_path_id``),
    in registry order, laid out in reading order from the grid origin."""
    shortcuts: list[DesktopShortcut] = []
    for row in rows:
        if row.internal or row.default_shortcut is None:
            continue
        launch = default_launch_path_id(row)
        if launch is None:
            continue
        shortcuts.append(
            DesktopShortcut(
                target=ShortcutTarget(app=row.name, launch=launch),
                mode=row.default_shortcut.mode,
                cell=reading_order_cell(len(shortcuts), SEED_GRID_COLUMNS),
            )
        )
    return tuple(shortcuts)


# Layouts: placements


@pure
def default_placement(window_id: WindowId, stored_count: int) -> WindowPlacement:
    """What a window with no placement reads as: the cascade frame at the bottom of the stack, minimized."""
    return WindowPlacement(
        window_id=window_id, frame=cascade_frame(stored_count), state=WindowState.NORMAL, is_minimized=True
    )


@pure
def opened_placement(window_id: WindowId, stored_count: int) -> WindowPlacement:
    """The placement an open writes for the requesting client: the cascade frame, normal, shown."""
    return WindowPlacement(
        window_id=window_id, frame=cascade_frame(stored_count), state=WindowState.NORMAL, is_minimized=False
    )


@pure
def drop_stale_placements(layout: DesktopLayout, live_window_ids: AbstractSet[WindowId]) -> DesktopLayout:
    """The layout without every placement naming a window the desktop no longer holds; the same object when none does."""
    kept = tuple(placement for placement in layout.placements if placement.window_id in live_window_ids)
    if len(kept) == len(layout.placements):
        return layout
    return layout.model_copy_update(to_update(layout.field_ref().placements, kept))


@pure
def effective_placements(layout: DesktopLayout, desktop: Desktop) -> tuple[WindowPlacement, ...]:
    """Every window of the desktop placed: the stored placements in their order (stale ones dropped), with the
    windows the layout lacks read as the default placement at the start of the list, in opening order."""
    live_ids = {window.id for window in desktop.windows}
    stored = tuple(placement for placement in layout.placements if placement.window_id in live_ids)
    stored_ids = {placement.window_id for placement in stored}
    missing = tuple(
        default_placement(window.id, len(stored)) for window in desktop.windows if window.id not in stored_ids
    )
    return (*missing, *stored)


@pure
def focused_window_id(placements: Sequence[WindowPlacement]) -> WindowId | None:
    """The last placement that is not minimized; None when the backdrop has focus."""
    return next((placement.window_id for placement in reversed(placements) if not placement.is_minimized), None)


@pure
def most_recently_focused_window_of_app(layout: DesktopLayout, desktop: Desktop, app: AppName) -> Window | None:
    """The window of ``app`` nearest the top of this client's stack, minimized or not, or None."""
    windows_by_id = {window.id: window for window in desktop.windows}
    for placement in reversed(effective_placements(layout, desktop)):
        window = windows_by_id.get(placement.window_id)
        if window is not None and window.app == app:
            return window
    return None


@pure
def placement_of(layout: DesktopLayout, window_id: WindowId) -> WindowPlacement:
    """The window's stored placement, else its default."""
    stored = next((placement for placement in layout.placements if placement.window_id == window_id), None)
    return stored if stored is not None else default_placement(window_id, len(layout.placements))


@pure
def _with_placement_on_top(layout: DesktopLayout, placement: WindowPlacement) -> DesktopLayout:
    others = tuple(candidate for candidate in layout.placements if candidate.window_id != placement.window_id)
    return layout.model_copy_update(to_update(layout.field_ref().placements, (*others, placement)))


@pure
def _with_placement_in_place(layout: DesktopLayout, placement: WindowPlacement) -> DesktopLayout:
    """The layout with the placement replacing its window's entry where it stands, or appended when there is none."""
    if not any(candidate.window_id == placement.window_id for candidate in layout.placements):
        return layout.model_copy_update(to_update(layout.field_ref().placements, (*layout.placements, placement)))
    return layout.model_copy_update(
        to_update(
            layout.field_ref().placements,
            tuple(
                placement if candidate.window_id == placement.window_id else candidate
                for candidate in layout.placements
            ),
        )
    )


@pure
def with_window_placed_on_open(layout: DesktopLayout, window_id: WindowId) -> DesktopLayout:
    """The layout with a just-opened window on top of the stack, at the cascade frame, shown."""
    return _with_placement_on_top(layout, opened_placement(window_id, len(layout.placements)))


@pure
def with_window_raised(layout: DesktopLayout, window_id: WindowId) -> DesktopLayout:
    """Focus: the window restored (un-minimized) and moved to the top of the stack."""
    current = placement_of(layout, window_id)
    return _with_placement_on_top(
        layout, current.model_copy_update(to_update(current.field_ref().is_minimized, False))
    )


@pure
def with_window_minimized(layout: DesktopLayout, window_id: WindowId) -> DesktopLayout:
    """Minimize: the window out of sight where it stands in the stack."""
    current = placement_of(layout, window_id)
    return _with_placement_in_place(
        layout, current.model_copy_update(to_update(current.field_ref().is_minimized, True))
    )


@pure
def with_window_restored(layout: DesktopLayout, window_id: WindowId) -> DesktopLayout:
    """Restore: the window shown at its own frame, normal, on top of the stack."""
    current = placement_of(layout, window_id)
    return _with_placement_on_top(
        layout,
        current.model_copy_update(
            to_update(current.field_ref().is_minimized, False),
            to_update(current.field_ref().state, WindowState.NORMAL),
        ),
    )


@pure
def with_window_state(layout: DesktopLayout, window_id: WindowId, state: WindowState) -> DesktopLayout:
    """Maximize or snap: the state set with the frame untouched, the window shown and raised."""
    current = placement_of(layout, window_id)
    return _with_placement_on_top(
        layout,
        current.model_copy_update(
            to_update(current.field_ref().is_minimized, False),
            to_update(current.field_ref().state, state),
        ),
    )


@pure
def with_window_frame(layout: DesktopLayout, window_id: WindowId, frame: Frame) -> DesktopLayout:
    """Place at a frame: the frame set with the state normal, the window shown and raised."""
    current = placement_of(layout, window_id)
    return _with_placement_on_top(
        layout,
        current.model_copy_update(
            to_update(current.field_ref().is_minimized, False),
            to_update(current.field_ref().state, WindowState.NORMAL),
            to_update(current.field_ref().frame, frame),
        ),
    )


@pure
def without_placement(layout: DesktopLayout, window_id: WindowId) -> DesktopLayout:
    """The layout without the window's placement; the same object when it has none."""
    if not any(candidate.window_id == window_id for candidate in layout.placements):
        return layout
    return layout.model_copy_update(
        to_update(
            layout.field_ref().placements,
            tuple(candidate for candidate in layout.placements if candidate.window_id != window_id),
        )
    )


@pure
def is_same_layout(first: DesktopLayout, second: DesktopLayout) -> bool:
    """Whether two layouts place the same windows the same way (the stamp aside)."""
    return first.placements == second.placements
