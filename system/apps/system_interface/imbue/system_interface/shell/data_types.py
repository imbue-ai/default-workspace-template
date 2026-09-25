from collections.abc import Mapping
from typing import Any
from typing import Final

from app_manifest.manifest import DefaultShortcut
from app_manifest.manifest import EntryMode
from app_manifest.manifest import LocationScope
from app_manifest.manifest import OPEN_LAUNCH_PATH_ID
from app_manifest.manifest import Pin
from app_manifest.manifest import PinStyle
from app_manifest.manifest import ShortcutMode
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from app_manifest.primitives import LaunchPathValue
from app_manifest.registry import RegistryLaunchPath
from app_manifest.registry import RegistryRow
from pydantic import AwareDatetime
from pydantic import Field
from pydantic import model_validator

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.primitives import ClientActivityKind
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import IfPresent
from imbue.system_interface.shell.primitives import LaunchTargetKind
from imbue.system_interface.shell.primitives import SaveId
from imbue.system_interface.shell.primitives import ShortcutTargetKind
from imbue.system_interface.shell.primitives import ShowOutcome
from imbue.system_interface.shell.primitives import UserId
from imbue.system_interface.shell.primitives import WallpaperKind
from imbue.system_interface.shell.primitives import WallpaperName
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowState
from imbue.system_interface.shell.primitives import WindowTitle


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


class UserRecord(FrozenModel):
    """What the shell keeps about one signed-in visitor: the desktop made for them (desktop plan section 3.10)."""

    user_id: UserId = Field(description="The account's user id, as the identity header carries it")
    desktop_id: DesktopId = Field(description="The desktop made for the user, where their new clients land")
    desktop_name: str = Field(
        description="That desktop's name as of the user's last arrival, for the notice when it is gone"
    )
    email: str | None = Field(default=None, description="The verified email as of the user's last arrival")
    display_name: str | None = Field(default=None, description="The display name as of the user's last arrival")
    last_seen: AwareDatetime = Field(description="When a client of the user last arrived")


class AppInventoryEntry(FrozenModel):
    """One app of the inventory: its registry row and whether it runs."""

    row: RegistryRow = Field(description="The registry row, validated on read")
    is_running: bool = Field(description="Derived from supervisord or a TCP probe, never stored")


@pure
def default_shortcut_wire_json(shortcut: DefaultShortcut | None) -> dict[str, str] | None:
    if shortcut is None:
        return None
    return {"launch": str(shortcut.launch), "mode": shortcut.mode.value}


@pure
def pin_wire_json(pin: Pin | None) -> dict[str, str] | None:
    if pin is None:
        return None
    return {
        "path": str(pin.path),
        "style": pin.style.value,
        "scope": pin.scope.value,
        "default_mode": pin.default_mode.value,
    }


@pure
def app_wire_json(entry: AppInventoryEntry) -> dict[str, Any]:
    """The ``app`` object of desktop contracts.md section 5.5."""
    row = entry.row
    return {
        "name": str(row.name),
        "display_name": str(row.display_name) if row.display_name is not None else str(row.name),
        "icon": row.icon or "",
        "label": row.label,
        "url": str(row.url),
        "internal": row.internal,
        "program": row.program or "",
        "critical": row.critical,
        "launch_paths": [launch_path_wire_json(launch_path) for launch_path in effective_launch_paths(row)],
        "default_shortcut": default_shortcut_wire_json(row.default_shortcut),
        "launcher_rank": row.launcher_rank,
        "pin": pin_wire_json(row.pin),
        "message_handlers": [
            {"type": str(handler.type), "path": str(handler.path)} for handler in row.message_handlers
        ],
        "is_running": entry.is_running,
    }


class AppPin(FrozenModel):
    """A registered app's pin: the app, and the table its manifest declares (pinned-taskbar-entries plan section 3.1)."""

    app: AppName = Field(description="The pinned app")
    pin: Pin = Field(description="The pin as the registry row carries it")


class ClientStateReport(FrozenModel):
    """The inbound ``client_state`` WebSocket message (desktop contracts.md section 6)."""

    client_id: ClientId = Field(description="The reporting client")
    active_desktop: DesktopId = Field(description="The desktop the client is on now")
    previous_desktop: str = Field(default="", description="The desktop it was on before, empty on connect")


class ClientActivityReport(FrozenModel):
    """The body of ``POST /api/client-activity`` (desktop contracts.md section 5.1): a message a client sent."""

    client_id: ClientId = Field(description="The client the activity belongs to")
    desktop_id: DesktopId = Field(description="The desktop the client was on")
    kind: ClientActivityKind = Field(description="A message sent to an app's page")
    app: str = Field(description="The app the message went to")
    key: str = Field(
        description="The marker of the page the message went to (a chat id); empty for a page without one"
    )
    text: str = Field(default="", description="The message text, truncated at write time")


class ClientReportOutcome(FrozenModel):
    """What recording a ``client_state`` report came to: the record, and whether its active desktop moved."""

    record: ClientRecord = Field(description="The client record as written")
    is_active_desktop_changed: bool = Field(
        description="Whether the stored active desktop differs from before the report"
    )


# The desktop model (desktop-interface contracts.md sections 4 and 5)


# The launch path every app that declares none has, at its root, synthesized by the shell (desktop
# contracts.md section 2). ``label`` is ``Open <display name>`` per app, filled in by ``effective_launch_paths``.
OPEN_LAUNCH_PATH_VALUE: Final[LaunchPathValue] = LaunchPathValue("/")


@pure
def effective_launch_paths(row: RegistryRow) -> tuple[RegistryLaunchPath, ...]:
    """The launch paths an app offers: its declared ones, or the synthesized ``open`` when it declares none."""
    if row.launch_paths:
        return row.launch_paths
    display = str(row.display_name) if row.display_name is not None else str(row.name)
    return (
        RegistryLaunchPath(
            id=OPEN_LAUNCH_PATH_ID, label=NonEmptyStr(f"Open {display}"), path=OPEN_LAUNCH_PATH_VALUE, params=()
        ),
    )


@pure
def launch_path_wire_json(launch_path: RegistryLaunchPath) -> dict[str, Any]:
    return {
        "id": str(launch_path.id),
        "label": str(launch_path.label),
        "path": str(launch_path.path),
        "method": launch_path.method.value,
        "params": [str(param) for param in launch_path.params],
        "presets": {str(name): value for name, value in launch_path.presets.items()},
        "text_param": str(launch_path.text_param) if launch_path.text_param is not None else None,
        "draft_param": str(launch_path.draft_param) if launch_path.draft_param is not None else None,
    }


# Fractions are computed in floating point; a frame that overshoots the unit square by a rounding error is not
# a frame outside it.
_FRAME_TOLERANCE: Final[float] = 1e-9


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
            raise InvalidShellValueError("a frame's values are fractions in 0..1")
        if self.x + self.width > 1.0 + _FRAME_TOLERANCE or self.y + self.height > 1.0 + _FRAME_TOLERANCE:
            raise InvalidShellValueError("a frame lies wholly inside the unit square")
        return self


class GridCell(FrozenModel):
    """One cell of the backdrop's shortcut grid."""

    column: int = Field(ge=0, description="Column from the grid origin")
    row: int = Field(ge=0, description="Row from the grid origin")


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


class Wallpaper(FrozenModel):
    """A reference to a wallpaper image: bundled with the shell, or a file in the workspace's wallpapers directory."""

    kind: WallpaperKind = Field(description="Bundled or file")
    name: WallpaperName = Field(description="The file name without its extension")


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


class DesktopsDocument(FrozenModel):
    """The whole of ``desktops.json``."""

    version: int = Field(description="The file format version")
    desktops: tuple[Desktop, ...] = Field(description="Every desktop, in creation order; the first is the fallback")


class WindowPlacement(FrozenModel):
    """Where one client keeps one window: its frame, state, and whether it is minimized."""

    window_id: WindowId = Field(description="The window placed")
    frame: Frame = Field(description="The frame, kept through every state so restore has somewhere to go")
    state: WindowState = Field(description="Normal, snapped to a half, or maximized")
    is_minimized: bool = Field(description="Whether the window is out of sight; orthogonal to the state")


class DesktopLayout(FrozenModel):
    """One client's ordered placements for one desktop (desktop contracts.md section 4.2); last is on top."""

    version: int = Field(description="The file format version")
    updated_at: AwareDatetime | None = Field(description="When last saved, None for a layout never written")
    placements: tuple[WindowPlacement, ...] = Field(description="Back to front")


class PlacementsSaveRequest(FrozenModel):
    """The body of ``POST /api/placements/<desktop_id>`` (desktop contracts.md section 5.4)."""

    client_id: ClientId = Field(description="The saving client")
    save_id: SaveId = Field(description="The save id the window minted, echoed in the placements_updated broadcast")
    base_updated_at: AwareDatetime | None = Field(
        default=None,
        description="The updated_at of the layout the window last fetched or saved; None for one only seen empty",
    )
    placements: tuple[WindowPlacement, ...] = Field(description="The whole layout, back to front")


class WindowOpenRequest(FrozenModel):
    """The body of ``POST /api/desktops/<id>/windows`` (desktop contracts.md section 5.3)."""

    app: AppName = Field(description="The app to open a page of")
    path: WindowPath = Field(description="The path under the app origin the page is at, query string included")
    client_id: ClientId = Field(description="The requesting client, whose placement is written at once")
    if_present: IfPresent = Field(
        default=IfPresent.FOCUS, description="Focus a window already at the path, or open another"
    )


class LaunchTarget(FrozenModel):
    """Where a launch's page goes (post-launch-paths plan section 3.3)."""

    kind: LaunchTargetKind = Field(description="A new window, a window already at the path, or a named window")
    window_id: WindowId | None = Field(
        default=None, description="The window this client points at the page; required for the window kind"
    )

    @model_validator(mode="after")
    def _check_window_named_for_the_window_kind(self) -> "LaunchTarget":
        if (self.kind is LaunchTargetKind.WINDOW) != (self.window_id is not None):
            raise InvalidShellValueError("a launch target names a window exactly when its kind is 'window'")
        return self


class LaunchRequest(FrozenModel):
    """The body of ``POST /api/desktops/<id>/launch`` (post-launch-paths plan section 5.3)."""

    app: AppName = Field(description="The app whose launch path runs")
    launch: LaunchPathId = Field(description="The launch path's id (the synthesized ``open`` included)")
    params: dict[str, str] = Field(default_factory=dict, description="The caller's values for the declared params")
    client_id: ClientId = Field(description="The requesting client, whose placement or page follows the launch")
    target: LaunchTarget = Field(description="Where the page the launch answers goes")
    minimized: bool = Field(
        default=False, description="Whether a window this launch opens is placed minimized for the client"
    )


class LaunchOutcome(FrozenModel):
    """What a launch came to: the window showing the page, the page's path, and whether the window was opened."""

    window: Window = Field(description="The window opened, focused, or navigated, as the requesting client sees it")
    path: WindowPath = Field(description="The page path the launch resolved to")
    is_new: bool = Field(description="True when a window was opened for the page")


class StoredWindowPath(FrozenModel):
    """One client's path and title for an independent window (pinned-taskbar-entries plan section 5.1)."""

    path: WindowPath = Field(description="Where the client's page of the window is")
    title: WindowTitle = Field(description="What that page calls itself; empty means the app's display name")


class WindowLocationReport(FrozenModel):
    """The body of ``POST /api/desktops/<id>/windows/<window_id>/location``."""

    client_id: ClientId = Field(
        description="The client whose page reported, whose own path an independent window keeps"
    )
    path: WindowPath = Field(description="Where the page is now")
    title: WindowTitle = Field(description="What the page calls itself now")


class ClientDesktopView(FrozenModel):
    """One desktop as one client sees it: the desktop, the client's layout of it, and its windows at the paths the
    client sees (an independent window at the client's own path)."""

    desktop: Desktop = Field(description="The shared record")
    layout: DesktopLayout = Field(description="The client's layout of the desktop, pinned windows placed")
    seen_windows: tuple[Window, ...] = Field(description="The desktop's windows as the client sees them")


class ShowChoice(FrozenModel):
    """What a ``show`` op settled on: how it shows the path, on which desktop, and the window (none for an open)."""

    outcome: ShowOutcome = Field(description="Raised, navigated, pinned, or opened")
    desktop_id: DesktopId = Field(description="The desktop the path is shown on")
    window: Window | None = Field(
        description="The window raised or navigated, as the client sees it; None to open one"
    )

    @model_validator(mode="after")
    def _check_a_window_exactly_unless_opening(self) -> "ShowChoice":
        if (self.window is None) != (self.outcome is ShowOutcome.OPENED):
            raise InvalidShellValueError(f"a show names a window unless it opens one, not {self.outcome.value}")
        return self


class WindowOpenOutcome(FrozenModel):
    """What an open came to: the window answered, and whether it was opened rather than found."""

    window: Window = Field(description="The window opened or focused")
    is_new: bool = Field(description="True for an open, False when an existing window was answered")


class DesktopChangeOutcome(FrozenModel):
    """What a desktop write that may change nothing came to: the desktop, and whether it was written."""

    desktop: Desktop = Field(description="The desktop after the edit")
    is_written: bool = Field(description="Whether the edit changed the desktop and was written")


class DesktopsChangeOutcome(FrozenModel):
    """What an edit over every desktop that may change nothing came to: the desktops, and whether they were written."""

    desktops: tuple[Desktop, ...] = Field(description="Every desktop after the edit, in creation order")
    is_written: bool = Field(description="Whether the edit changed a desktop and the file was written")


class PlacementsEditOutcome(FrozenModel):
    """What editing a client's layout of a desktop under the state lock came to."""

    layout: DesktopLayout = Field(description="The layout after the edit, stamped when it was written")
    is_written: bool = Field(description="Whether the edit changed the layout and was written")


class ClientArrivalOutcome(FrozenModel):
    """What a client's arrival came to: the desktop it lands on, and the desktop made for its user when one was."""

    desktop_id: DesktopId = Field(description="Where the client lands")
    created_desktop: Desktop | None = Field(description="The desktop seeded for a first-time user, else None")
    replaced_desktop_name: str | None = Field(
        description="The name of the user's earlier desktop when it had been deleted and a fresh one was seeded"
    )


class DesktopDeleteOutcome(FrozenModel):
    """What deleting a desktop came to: the desktop that went, and where its clients fall back to."""

    deleted: Desktop = Field(description="The desktop as it was")
    fallback_desktop_id: DesktopId = Field(description="The first remaining desktop")


@pure
def desktop_wire_json(
    desktop: Desktop, client_paths: Mapping[WindowId, Mapping[ClientId, WindowPath]]
) -> dict[str, Any]:
    """The ``desktop`` object of desktop contracts.md section 5.2: the record of section 4.1 with each window
    carrying ``client_paths``, the path each client's page of an independent window is at (empty for a linked
    window, and for a client at the home path), so a reader of the shell's windows sees what every client shows."""
    return {
        **desktop.model_dump(mode="json"),
        "windows": [window_wire_json(window, client_paths.get(window.id, {})) for window in desktop.windows],
    }


@pure
def window_wire_json(window: Window, client_paths: Mapping[ClientId, WindowPath]) -> dict[str, Any]:
    """The ``window`` object of desktop contracts.md section 5.2 (section 4.1's record plus ``client_paths``)."""
    return {
        **window.model_dump(mode="json"),
        "client_paths": {str(client_id): str(path) for client_id, path in client_paths.items()},
    }


@pure
def desktop_layout_wire_json(
    layout: DesktopLayout, window_paths: Mapping[WindowId, StoredWindowPath]
) -> dict[str, Any]:
    """The ``layout`` object of desktop contracts.md section 4.2, with the client's stored paths for the desktop's
    independent windows (pinned-taskbar-entries plan section 5.3)."""
    return {
        **layout.model_dump(mode="json"),
        "window_paths": {str(window_id): stored.model_dump(mode="json") for window_id, stored in window_paths.items()},
    }


@pure
def effective_window(window: Window, stored: StoredWindowPath | None) -> Window:
    """The window as one client sees it: an independent window wears the client's stored path and title (the home
    path with no title when it has none); a linked window is the shared record."""
    if window.scope is LocationScope.LINKED or stored is None:
        return window
    return window.model_copy_update(
        to_update(window.field_ref().path, stored.path), to_update(window.field_ref().title, stored.title)
    )
