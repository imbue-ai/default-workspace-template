"""The desktop's records (desktop contracts.md section 4): what the shell keeps in its state files, and the views of
them its answers carry (section 5)."""

from typing import Final

from app_manifest.manifest import EntryMode
from app_manifest.manifest import LocationScope
from app_manifest.manifest import PinStyle
from app_manifest.manifest import ShortcutMode
from app_manifest.manifest import describe_validation_error
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from pydantic import AwareDatetime
from pydantic import Field
from pydantic import ValidationError
from pydantic import model_validator

from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import ShortcutTargetKind
from workspace_layout.primitives import UserId
from workspace_layout.primitives import WallpaperKind
from workspace_layout.primitives import WallpaperName
from workspace_layout.primitives import WindowId
from workspace_layout.primitives import WindowPath
from workspace_layout.primitives import WindowState
from workspace_layout.primitives import WindowTitle

# Fractions are computed in floating point; a frame that overshoots the unit square by a rounding error is not
# a frame outside it.
_FRAME_TOLERANCE: Final[float] = 1e-9
_FRAME_COMPONENT_COUNT: Final[int] = 4
_CELL_COMPONENT_COUNT: Final[int] = 2


class Frame(FrozenModel):
    """A window's rectangle in fractions of the backdrop, wholly inside the unit square."""

    x: float = Field(description="Left edge, 0..1")
    y: float = Field(description="Top edge, 0..1")
    width: float = Field(description="Width, 0..1")
    height: float = Field(description="Height, 0..1")

    @model_validator(mode="after")
    def _check_inside_the_unit_square(self) -> "Frame":
        if not (
            0.0 <= self.x <= 1.0 and 0.0 <= self.y <= 1.0 and 0.0 <= self.width <= 1.0 and 0.0 <= self.height <= 1.0
        ):
            raise InvalidLayoutValueError("a frame's values are fractions in 0..1")
        if self.x + self.width > 1.0 + _FRAME_TOLERANCE or self.y + self.height > 1.0 + _FRAME_TOLERANCE:
            raise InvalidLayoutValueError("a frame lies wholly inside the unit square")
        return self


@pure
def parse_frame(raw: str) -> Frame:
    """A frame from its text form, ``x,y,width,height`` in fractions; raises InvalidLayoutValueError."""
    parts = raw.split(",")
    if len(parts) != _FRAME_COMPONENT_COUNT:
        raise InvalidLayoutValueError(f"a frame is 'x,y,width,height' in fractions, not {raw!r}")
    try:
        x, y, width, height = (float(part) for part in parts)
        return Frame(x=x, y=y, width=width, height=height)
    except ValidationError as e:
        raise InvalidLayoutValueError(
            f"a frame is 'x,y,width,height' in fractions inside the unit square: {describe_validation_error(e)}"
        ) from e
    except ValueError as e:
        raise InvalidLayoutValueError(f"a frame is 'x,y,width,height' in fractions inside the unit square: {e}") from e


class GridCell(FrozenModel):
    """One cell of the backdrop's shortcut grid."""

    column: int = Field(ge=0, description="Column from the grid origin")
    row: int = Field(ge=0, description="Row from the grid origin")


@pure
def parse_cell(raw: str) -> GridCell:
    """A grid cell from its text form, ``column,row``; raises InvalidLayoutValueError."""
    parts = raw.split(",")
    if len(parts) != _CELL_COMPONENT_COUNT:
        raise InvalidLayoutValueError(f"a cell is 'column,row', not {raw!r}")
    try:
        column, row = (int(part) for part in parts)
        return GridCell(column=column, row=row)
    except ValidationError as e:
        raise InvalidLayoutValueError(
            f"a cell is 'column,row' with both at least zero: {describe_validation_error(e)}"
        ) from e
    except ValueError as e:
        raise InvalidLayoutValueError(f"a cell is 'column,row' with both at least zero: {e}") from e


class Wallpaper(FrozenModel):
    """A reference to a wallpaper image: bundled with the shell, or a file in the workspace's wallpapers directory."""

    kind: WallpaperKind = Field(description="Bundled or file")
    name: WallpaperName = Field(description="The file name without its extension")


class ShortcutTarget(FrozenModel):
    """What a desktop shortcut runs: a launch path of an app (the only V1 kind)."""

    kind: ShortcutTargetKind = Field(default=ShortcutTargetKind.LAUNCH, description="The target kind")
    app: AppName = Field(description="The registered app")
    launch: LaunchPathId = Field(description="The launch path id, or 'open' for an app that declares none")


class DesktopShortcut(FrozenModel):
    """One shortcut on a desktop's backdrop: a launch path, in focus or new mode, in one grid cell."""

    target: ShortcutTarget = Field(description="What the shortcut runs")
    mode: ShortcutMode = Field(description="Focus the app's most recent window first, or always run the launch path")
    cell: GridCell = Field(description="The stored cell; rendering fits it to the current grid")


class Window(FrozenModel):
    """One app page on one desktop: the app, the path the page is at, and the title it last reported; shared."""

    id: WindowId = Field(description="Minted by the shell when the window was opened, never reused")
    app: AppName = Field(description="The app whose page the window shows")
    path: WindowPath = Field(description="The path under the app origin the page is at (with its query string)")
    title: WindowTitle = Field(description="What the page last reported; empty means the app's display name")
    opened_at: AwareDatetime = Field(description="When the window was opened")
    is_pinned: bool = Field(
        default=False, description="Whether this is the app's pinned window on the desktop: permanent, never closed"
    )
    scope: LocationScope = Field(
        default=LocationScope.LINKED,
        description="Whether every client follows the shared path and title, or each client keeps its own",
    )


class Desktop(FrozenModel):
    """A named, shared collection of windows and shortcuts over a wallpaper (desktop contracts.md section 4.1)."""

    id: DesktopId = Field(description="The slugified name, stable across renames")
    name: str = Field(description="Free-form name shown in the UI")
    color: str = Field(description="Accent colour as a '#RRGGBB' string")
    glyph: int = Field(description="Index into the frontend's glyph table")
    wallpaper: Wallpaper | None = Field(description="The backdrop image; None draws the theme's default")
    shortcuts: tuple[DesktopShortcut, ...] = Field(description="At most one per (app, launch), in insertion order")
    windows: tuple[Window, ...] = Field(description="Every window on the desktop, in opening order")


class WindowPlacement(FrozenModel):
    """Where one client keeps one window: its frame, state, and whether it is minimized."""

    window_id: WindowId = Field(description="The window placed")
    frame: Frame = Field(description="The frame, kept through every state so restore has somewhere to go")
    state: WindowState = Field(description="Normal, snapped to a half, or maximized")
    is_minimized: bool = Field(description="Whether the window is out of sight; orthogonal to the state")
    is_detached: bool = Field(
        default=False,
        description=(
            "Whether the window is pulled out into a desktop window of the embedding chrome's own (the "
            "pull-out-window spec): a ghost at its frame here, its page shown there; orthogonal to the state"
        ),
    )


class DesktopLayout(FrozenModel):
    """One client's ordered placements for one desktop (desktop contracts.md section 4.2); last is on top."""

    version: int = Field(description="The file format version")
    updated_at: AwareDatetime | None = Field(description="When last saved, None for a layout never written")
    placements: tuple[WindowPlacement, ...] = Field(description="Back to front")


class StoredWindowPath(FrozenModel):
    """One client's path and title for an independent window (pinned-taskbar-entries plan section 5.1)."""

    path: WindowPath = Field(description="Where the client's page of the window is")
    title: WindowTitle = Field(description="What that page calls itself; empty means the app's display name")


class FloatingPosition(FrozenModel):
    """Where a client keeps a floating entry: the top-left corner of its box, in fractions of the backdrop."""

    x: float = Field(ge=0.0, le=1.0, description="Left edge, 0..1")
    y: float = Field(ge=0.0, le=1.0, description="Top edge, 0..1")


class EntryPresentation(FrozenModel):
    """How one client shows one pinned entry (pinned-taskbar-entries plan section 3.4); global across desktops."""

    mode: EntryMode = Field(description="In the taskbar, or floating above the windows")
    style: PinStyle = Field(description="Plain, or the style the pin declares")
    position: FloatingPosition | None = Field(default=None, description="The floating position; None for the default")


class ClientRecord(FrozenModel):
    """What the shell keeps about one browser context (desktop contracts.md section 4.3)."""

    id: ClientId = Field(description="The client's stored id")
    active_desktop: DesktopId | None = Field(default=None, description="The desktop the client is on")
    last_seen: AwareDatetime = Field(description="When the client last arrived or reported")
    user_id: UserId | None = Field(
        default=None,
        description="The signed-in visitor the client last arrived as; None for the owner or an anonymous client",
    )
    entries: dict[str, EntryPresentation] = Field(
        default_factory=dict, description="The client's presentation of each pinned entry, by app name"
    )
    shown_history: tuple[str, ...] = Field(
        default=(),
        description="What the client has shown on the phone layout, most recent last: window ids and 'home', "
        "each at most once",
    )
    desktop_revision: int = Field(
        default=0,
        ge=0,
        description="Counts the moves of the stored active desktop and the reports redirected off a deleted desktop: "
        "orders the client's desktop news",
    )


# The views the shell's answers carry (desktop contracts.md section 5)


class WindowView(Window):
    """The ``window`` object of desktop contracts.md section 5.2: the record, with the path each client's page of an
    independent window is at (empty for a linked window, and for a client at the home path)."""

    client_paths: dict[ClientId, WindowPath] = Field(description="Each client's own path, by client id")


class DesktopView(Desktop):
    """The ``desktop`` object of desktop contracts.md section 5.2: the record, its windows as views."""

    windows: tuple[WindowView, ...] = Field(description="Every window on the desktop, in opening order")


class DesktopLayoutView(DesktopLayout):
    """The ``layout`` object of desktop contracts.md section 4.2, with the client's stored paths for the desktop's
    independent windows (pinned-taskbar-entries plan section 5.3)."""

    window_paths: dict[WindowId, StoredWindowPath] = Field(description="The client's path of each independent window")
