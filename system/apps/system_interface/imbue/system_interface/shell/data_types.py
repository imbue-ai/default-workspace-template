from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final
from typing import Literal

from app_manifest.manifest import LocationScope
from app_manifest.manifest import OPEN_LAUNCH_PATH_ID
from app_manifest.manifest import Pin
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from app_manifest.primitives import LaunchPathValue
from app_manifest.registry import RegistryLaunchPath
from app_manifest.registry import RegistryRow
from pydantic import AwareDatetime
from pydantic import Field
from pydantic import model_validator
from workspace_layout.answers import InventoryApp
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import IfPresent
from workspace_layout.primitives import ShowOutcome
from workspace_layout.primitives import UserId
from workspace_layout.primitives import WindowId
from workspace_layout.primitives import WindowPath
from workspace_layout.primitives import WindowTitle
from workspace_layout.records import ClientRecord
from workspace_layout.records import Desktop
from workspace_layout.records import DesktopLayout
from workspace_layout.records import DesktopLayoutView
from workspace_layout.records import DesktopView
from workspace_layout.records import StoredWindowPath
from workspace_layout.records import Window
from workspace_layout.records import WindowPlacement
from workspace_layout.records import WindowView

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.primitives import LaunchTargetKind
from imbue.system_interface.shell.primitives import PageId
from imbue.system_interface.shell.primitives import ReportId
from imbue.system_interface.shell.primitives import SaveId


class ClientShownRequest(FrozenModel):
    """The body of ``POST /api/clients/<client_id>/shown``: the window the client now shows, or None for home."""

    window_id: WindowId | None = Field(description="The window shown, or None for the home grid")


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
def stoppable_program_of(entry: AppInventoryEntry, entries: Sequence[AppInventoryEntry]) -> str | None:
    """The supervised program the workspace may stop, start, park, and wake for this app, or None: an app with no
    program, a critical app, and any row inside a critical app's program are never acted on (desktop contracts.md
    section 5.1)."""
    program = entry.row.program or ""
    if not program or entry.row.critical:
        return None
    if any(other.row.critical and other.row.program == program for other in entries):
        return None
    return program


@pure
def app_view(entry: AppInventoryEntry, share_domain: str | None) -> InventoryApp:
    """The ``app`` object of desktop contracts.md section 5.5; ``share_domain`` is the domain the workspace was last
    shared under, if it ever was, on which an app with a label has an address."""
    row = entry.row
    return InventoryApp(
        name=row.name,
        display_name=str(row.display_name) if row.display_name is not None else str(row.name),
        icon=row.icon or "",
        label=row.label,
        url=row.url,
        internal=row.internal,
        program=row.program or "",
        critical=row.critical,
        stop_when_no_windows=row.stop_when_no_windows,
        launch_paths=effective_launch_paths(row),
        default_shortcut=row.default_shortcut,
        launcher_rank=row.launcher_rank,
        pin=row.pin,
        message_handlers=row.message_handlers,
        is_running=entry.is_running,
        share_url=f"https://{row.label}.{share_domain}/" if share_domain is not None and row.label else None,
    )


class AppPin(FrozenModel):
    """A registered app's pin: the app, and the table its manifest declares (pinned-taskbar-entries plan section 3.1)."""

    app: AppName = Field(description="The pinned app")
    pin: Pin = Field(description="The pin as the registry row carries it")


class ClientStateReport(FrozenModel):
    """The inbound ``client_state`` WebSocket message (desktop contracts.md section 6)."""

    client_id: ClientId = Field(description="The reporting client")
    active_desktop: DesktopId = Field(description="The desktop the client is on now")
    previous_desktop: str = Field(
        default="",
        description="The desktop the window left, empty when the report names none (a connect, a landing, or a "
        "following report)",
    )
    report_id: ReportId | None = Field(
        default=None,
        description="The window's id for a report that moves the client, echoed on the broadcast it causes; None for a "
        "following report",
    )
    is_following: bool = Field(
        default=False,
        description="Whether the window only followed the client's stored desktop (pushed, or read on a reconnect): "
        "the connection is registered on it and the record is not moved",
    )
    page_id: PageId | None = Field(
        default=None, description="The reporting page's id, which tells its own desktop moves from the others'"
    )
    revision: int | None = Field(
        default=None,
        ge=0,
        description="For a report that moves the client, the newest desktop revision the page had heard when it made "
        "the report; None for a following report",
    )


class PopOutStateReport(FrozenModel):
    """The inbound ``client_state`` WebSocket message of a pop-out's page (desktop contracts.md section 6): the
    client it belongs to, and no desktop, since the client's active desktop is its main window's."""

    client_id: ClientId = Field(description="The client the pop-out belongs to")
    is_pop_out: Literal[True] = Field(description="Marks the report as a pop-out's")


class ClientReportOutcome(FrozenModel):
    """What recording a ``client_state`` report came to: the record, and whether its active desktop moved."""

    record: ClientRecord = Field(description="The client record as written")
    is_active_desktop_changed: bool = Field(
        description="Whether the stored active desktop differs from before the report"
    )
    is_superseded: bool = Field(
        default=False,
        description="Whether the report was made before a move its page had not heard of, and so recorded nothing",
    )


# The desktop model (desktop-interface contracts.md sections 4 and 5)


# The launch path every app that declares none has, at its root, synthesized by the shell (desktop
# contracts.md section 2). Its ``label`` is the app's display name, filled in by
# ``effective_launch_paths``: the row stands in a list beside rows an app labelled for itself, where
# a verb reads as a different KIND of row rather than as the same row with a word in front of it.
OPEN_LAUNCH_PATH_VALUE: Final[LaunchPathValue] = LaunchPathValue("/")


@pure
def effective_launch_paths(row: RegistryRow) -> tuple[RegistryLaunchPath, ...]:
    """The launch paths an app offers: its declared ones, or the synthesized ``open`` when it declares none."""
    if row.launch_paths:
        return row.launch_paths
    display = str(row.display_name) if row.display_name is not None else str(row.name)
    return (
        RegistryLaunchPath(id=OPEN_LAUNCH_PATH_ID, label=NonEmptyStr(display), path=OPEN_LAUNCH_PATH_VALUE, params=()),
    )


class DesktopsDocument(FrozenModel):
    """The whole of ``desktops.json``."""

    version: int = Field(description="The file format version")
    desktops: tuple[Desktop, ...] = Field(description="Every desktop, in creation order; the first is the fallback")


class DefaultShortcutsOfferedDocument(FrozenModel):
    """The whole of ``default_shortcuts_offered.json``: the apps whose default shortcut the shell has offered."""

    version: int = Field(description="The file format version")
    apps: tuple[AppName, ...] = Field(
        description="Every app whose default shortcut the shell has put on a desktop or found there, sorted"
    )


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
    minimized: bool = Field(
        default=False, description="Whether a window this open creates is placed minimized for the client"
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
def window_view(window: Window, client_paths: Mapping[ClientId, WindowPath]) -> WindowView:
    """The ``window`` object of desktop contracts.md section 5.2: the record, with each client's own path."""
    return WindowView.model_validate({**dict(window), "client_paths": dict(client_paths)})


@pure
def desktop_view(desktop: Desktop, client_paths: Mapping[WindowId, Mapping[ClientId, WindowPath]]) -> DesktopView:
    """The ``desktop`` object of desktop contracts.md section 5.2: the record of section 4.1 with each window
    carrying ``client_paths``, the path each client's page of an independent window is at (empty for a linked
    window, and for a client at the home path), so a reader of the shell's windows sees what every client shows."""
    windows = tuple(window_view(window, client_paths.get(window.id, {})) for window in desktop.windows)
    return DesktopView.model_validate({**dict(desktop), "windows": windows})


@pure
def desktop_layout_view(layout: DesktopLayout, window_paths: Mapping[WindowId, StoredWindowPath]) -> DesktopLayoutView:
    """The ``layout`` object of desktop contracts.md section 4.2, with the client's stored paths for the desktop's
    independent windows (pinned-taskbar-entries plan section 5.3)."""
    return DesktopLayoutView.model_validate({**dict(layout), "window_paths": dict(window_paths)})


@pure
def effective_window(window: Window, stored: StoredWindowPath | None) -> Window:
    """The window as one client sees it: an independent window wears the client's stored path and title (the home
    path with no title when it has none); a linked window is the shared record."""
    if window.scope is LocationScope.LINKED or stored is None:
        return window
    return window.model_copy_update(
        to_update(window.field_ref().path, stored.path), to_update(window.field_ref().title, stored.title)
    )
