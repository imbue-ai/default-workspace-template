from typing import Any
from typing import Final

from app_instances.data_types import InstanceLifetime
from app_instances.data_types import InstanceRecord
from app_instances.data_types import InstanceStatus
from app_instances.primitives import InstanceKey
from app_manifest.manifest import DefaultShortcut
from app_manifest.manifest import OPEN_LAUNCH_PATH_ID
from app_manifest.manifest import ShortcutMode
from app_manifest.primitives import ActionId
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from app_manifest.primitives import LaunchPathValue
from app_manifest.registry import RegistryAction
from app_manifest.registry import RegistryLaunchPath
from app_manifest.registry import RegistryRow
from loguru import logger
from pydantic import AwareDatetime
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError
from pydantic import model_validator

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import ClientActivityKind
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import IfPresent
from imbue.system_interface.shell.primitives import ProjectId
from imbue.system_interface.shell.primitives import SaveId
from imbue.system_interface.shell.primitives import SharingMode
from imbue.system_interface.shell.primitives import ShortcutTargetKind
from imbue.system_interface.shell.primitives import TabId
from imbue.system_interface.shell.primitives import ViewId
from imbue.system_interface.shell.primitives import WallpaperKind
from imbue.system_interface.shell.primitives import WallpaperName
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowState
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.shell.primitives import address_for


class Shortcut(FrozenModel):
    """One rail entry of a project: an app's action, in focus or new mode."""

    app: AppName = Field(description="The registered app")
    action: ActionId = Field(description="The action the row runs")
    mode: ShortcutMode = Field(description="Focus the app's most recent tab first, or always run the action")


class Project(FrozenModel):
    """A named view: its display metadata, its shared tab set, and its shortcuts (contracts.md section 6)."""

    id: ProjectId = Field(description="The slugified name, stable across renames")
    name: str = Field(description="Free-form name shown in the UI")
    color: str = Field(description="Accent color as a '#RRGGBB' string")
    glyph: int = Field(description="Index into the frontend's squiggle glyph table")
    tabs: tuple[Address, ...] = Field(description="Every instance the project shows, in the order added")
    shortcuts: tuple[Shortcut, ...] = Field(description="The rail rows, in rail order")


# The ``kind`` the ``params`` of a dockview panel showing an instance carry (contracts.md section 6). A
# launcher panel (the New Tab page) carries another kind and names no instance, so the shell never looks for it.
INSTANCE_PANEL_KIND: Final[str] = "instance"


class InstancePanelParams(FrozenModel):
    """The ``params`` dockview stores on a panel showing an instance: the one place a tab's identity lives (contracts.md section 6)."""

    # The browser owns this object and may add keys the shell does not know; reading tolerates them, and
    # the shell edits the stored dict itself (``with_panel_params_address``, which keeps every other key)
    # rather than round-tripping it through this model.
    model_config = ConfigDict(extra="ignore")

    address: Address = Field(description="The instance the panel shows")
    tab_id: TabId = Field(
        alias="tabId",
        description="The page's id: minted when the page was first opened, shared by every panel showing it",
    )
    last_focused_ms: int = Field(
        default=0,
        alias="lastFocusedMs",
        description="Epoch milliseconds the panel was last the active one, 0 when never",
    )


@pure
def _panel_entries(dockview: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    panels = dockview.get("panels") if dockview is not None else None
    if not isinstance(panels, dict):
        return {}
    return {panel_id: entry for panel_id, entry in panels.items() if isinstance(entry, dict)}


def instance_panel_params_by_id(dockview: dict[str, Any] | None) -> dict[str, InstancePanelParams]:
    """Each instance panel's params, keyed by dockview panel id. Launchers are skipped; an instance panel whose params
    do not parse is skipped with a warning, so a damaged entry costs one tab rather than the whole arrangement."""
    parsed: dict[str, InstancePanelParams] = {}
    for panel_id, entry in _panel_entries(dockview).items():
        params = entry.get("params")
        if not isinstance(params, dict) or params.get("kind") != INSTANCE_PANEL_KIND:
            continue
        try:
            parsed[panel_id] = InstancePanelParams.model_validate(params)
        except ValidationError as e:
            logger.warning("Skipped panel {} with unreadable params: {}", panel_id, e.errors()[0]["msg"])
    return parsed


@pure
def instance_panel_params_json(address: Address, tab_id: TabId, last_focused_ms: int) -> dict[str, Any]:
    """The ``params`` entry the shell writes for an instance panel, in the browser's spelling."""
    return {
        "kind": INSTANCE_PANEL_KIND,
        "address": str(address),
        "tabId": str(tab_id),
        "lastFocusedMs": last_focused_ms,
    }


@pure
def with_panel_params_address(dockview: dict[str, Any], panel_id: str, address: Address) -> dict[str, Any]:
    """The document with the params of ``panel_id`` pointed at ``address``, every other key of the params kept."""
    panels = dockview["panels"]
    entry = panels[panel_id]
    return {
        **dockview,
        "panels": {**panels, panel_id: {**entry, "params": {**entry["params"], "address": str(address)}}},
    }


# CLEANUP: drop this fold, the ``_fold_legacy_tabs`` validators on ``LayoutRecord`` and ``LayoutSaveRequest`` that
# call it, the ``tabs`` mention in the docstrings that cite it, and the two tests of the older shape
# (``test_a_layout_in_the_older_shape_reads_as_params_only`` in data_types_test.py and
# ``test_a_save_in_the_older_shape_is_folded_into_the_panels_params`` in routes_test.py) once every workspace has
# saved a layout with a shell from after the workspace app model's params-only layout files: a file written by
# the older shell carried a ``tabs`` block beside the dockview document, and that block was the truth of each
# panel's identity.
@pure
def fold_legacy_tabs_into_dockview(data: Any) -> Any:
    """A layout body in the older shape, with its ``tabs`` block folded into each panel's ``params``; any other value unchanged."""
    if not isinstance(data, dict) or "tabs" not in data:
        return data
    without_tabs = {key: value for key, value in data.items() if key != "tabs"}
    tabs = data["tabs"]
    dockview = without_tabs.get("dockview")
    if not isinstance(tabs, dict) or not isinstance(dockview, dict) or not isinstance(dockview.get("panels"), dict):
        return without_tabs
    panels = dict(dockview["panels"])
    for panel_id, tab in tabs.items():
        entry = panels.get(panel_id)
        if not isinstance(tab, dict) or not isinstance(entry, dict):
            continue
        panels[panel_id] = {
            **entry,
            "params": {
                "kind": INSTANCE_PANEL_KIND,
                "address": tab.get("address"),
                "tabId": tab.get("tab_id"),
                "lastFocusedMs": tab.get("last_focused_ms", 0),
            },
        }
    return {**without_tabs, "dockview": {**dockview, "panels": panels}}


class LayoutRecord(FrozenModel):
    """One client's arrangement of one view (contracts.md section 6): dockview's own document, whose per-panel ``params`` name what each tab shows."""

    dockview: dict[str, Any] | None = Field(description="The serialized dockview grid, None for a never-arranged view")
    device_kind: DeviceKind = Field(description="The device kind the arrangement was made on")
    updated_at: AwareDatetime | None = Field(
        description="When the arrangement was last saved, None for the empty layout"
    )

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_tabs(cls, data: Any) -> Any:
        return fold_legacy_tabs_into_dockview(data)


class ClientRecord(FrozenModel):
    """What the shell keeps about one browser context (contracts.md section 7, and desktop contracts.md section 4.3).

    A client of the tabbed shell reports a view and a device kind; a client of the desktop shell reports a
    desktop. The record carries whichever it has been told, and both once it has been told both.
    """

    id: ClientId = Field(description="The client's stored id")
    # CLEANUP: drop ``device_kind`` and ``active_view`` (and bump ``clients.json`` to version 2) once the
    # desktop shell has replaced the tabbed one (desktop-interface plan, phase 6).
    device_kind: DeviceKind = Field(default=DeviceKind.DESKTOP, description="Desktop or mobile")
    active_view: ViewId | None = Field(default=None, description="The view the client is on in the tabbed shell")
    active_desktop: DesktopId | None = Field(default=None, description="The desktop the client is on")
    last_seen: AwareDatetime = Field(description="When the client last reported")


class InventoryInstance(FrozenModel):
    """One instance as the shell lists it: the app's record plus its address; the synthesized record of a single-instance app has an empty key."""

    key: str = Field(description="The app-scoped key; empty for a single-instance app's one record")
    url: str = Field(description="Where the instance's page is, as a path under the app's origin")
    title: str = Field(description="What users see")
    status: InstanceStatus = Field(description="What the instance is doing")
    lifetime: InstanceLifetime = Field(description="Whether it lives until deleted or only while referenced")
    last_active: AwareDatetime | None = Field(description="When it was last active, None when unknown")
    renameable: bool = Field(description="Whether the rename route is accepted")
    stoppable: bool = Field(description="Whether the stop and start routes are accepted")

    @pure
    def address(self, app: AppName) -> Address:
        return address_for(app, None if self.key == "" else InstanceKey(self.key))


@pure
def inventory_instance_from_record(record: InstanceRecord) -> InventoryInstance:
    return InventoryInstance(
        key=str(record.key),
        url=str(record.url),
        title=str(record.title),
        status=record.status,
        lifetime=record.lifetime,
        last_active=record.last_active,
        renameable=record.renameable,
        stoppable=record.stoppable,
    )


@pure
def synthesized_single_instance(row: RegistryRow, is_running: bool) -> InventoryInstance:
    """The one record a single-instance app carries (contracts.md section 8)."""
    return InventoryInstance(
        key="",
        url="/",
        title=str(row.display_name) if row.display_name is not None else str(row.name),
        status=InstanceStatus.IDLE if is_running else InstanceStatus.STOPPED,
        lifetime=InstanceLifetime.EXPLICIT,
        last_active=None,
        # The app-level Stop and Start are the single-instance app's; its one record has none of its own.
        renameable=False,
        stoppable=False,
    )


class AppInventoryEntry(FrozenModel):
    """One app of the inventory: its registry row, whether it runs, and its instances as last fetched."""

    row: RegistryRow = Field(description="The registry row, validated on read")
    is_running: bool = Field(description="Derived from supervisord or a TCP probe, never stored")
    instances: tuple[InventoryInstance, ...] = Field(description="The app's instances, in the app's list order")
    # False until the app's instances API has answered a list once (a single-instance app's one
    # record is synthesized, so it counts as listed): an empty list that was never fetched is
    # not evidence that an address is missing, and a client shows nothing as unavailable on it.
    is_listed: bool = Field(description="Whether the instance list is the app's own answer rather than the seed")
    # A record the shell has held for less than the grace period is not deleted for being
    # unreferenced: the create that made it has returned but the tab docking it may not have
    # been saved yet.
    first_seen_at_by_key: dict[str, float] = Field(
        description="Monotonic seconds each key was first listed, for the referenced-deletion grace"
    )

    @pure
    def address_of(self, instance: InventoryInstance) -> Address:
        return instance.address(self.row.name)

    @pure
    def addresses(self) -> list[Address]:
        return [self.address_of(instance) for instance in self.instances]


@pure
def app_wire_json(entry: AppInventoryEntry) -> dict[str, Any]:
    """The ``app`` object of contracts.md section 8."""
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
        "instances_url": instances_url_of(row),
        "has_instances": row.instances,
        "actions": [action_wire_json(action) for action in effective_actions(row)],
        "launch_paths": [launch_path_wire_json(launch_path) for launch_path in effective_launch_paths(row)],
        "default_shortcut": default_shortcut_wire_json(row.default_shortcut),
        "launcher_rank": row.launcher_rank,
        "is_running": entry.is_running,
        "is_listed": entry.is_listed,
        "instances": [instance.model_dump(mode="json") for instance in entry.instances],
    }


@pure
def instances_url_of(row: RegistryRow) -> str:
    """Where the app's instances API is reached: its ``instances_url``, else its ``url`` (contracts.md section 3)."""
    return str(row.instances_url) if row.instances_url is not None else str(row.url)


@pure
def action_wire_json(action: RegistryAction) -> dict[str, Any]:
    return {"id": str(action.id), "label": str(action.label), "params": [str(param) for param in action.params]}


@pure
def default_shortcut_wire_json(shortcut: DefaultShortcut | None) -> dict[str, str | None] | None:
    if shortcut is None:
        return None
    return {
        "action": str(shortcut.action),
        "launch": str(shortcut.launch) if shortcut.launch is not None else None,
        "mode": shortcut.mode.value,
    }


# The one action every single-instance app has, synthesized by the shell (contracts.md section 2).
OPEN_ACTION: RegistryAction = RegistryAction(id=ActionId("open"), label=NonEmptyStr("Open"))


@pure
def effective_actions(row: RegistryRow) -> tuple[RegistryAction, ...]:
    """The actions an app offers: its declared ones, or the synthesized ``open`` for a single-instance app."""
    if row.instances:
        return row.actions
    display = str(row.display_name) if row.display_name is not None else str(row.name)
    return (RegistryAction(id=OPEN_ACTION.id, label=NonEmptyStr(f"Open {display}")),)


class ClientStateReport(FrozenModel):
    """The inbound ``client_state`` WebSocket message, in the tabbed shell's shape (contracts.md section 8) or the
    desktop shell's (desktop contracts.md section 6); a report names a view, a desktop, or both."""

    client_id: ClientId = Field(description="The reporting client")
    # CLEANUP: drop ``device_kind``, ``active_view``, and ``previous_view`` once the desktop shell has replaced
    # the tabbed one (desktop-interface plan, phase 6).
    device_kind: DeviceKind = Field(default=DeviceKind.DESKTOP, description="Desktop or mobile")
    active_view: ViewId | None = Field(default=None, description="The view the client is on now")
    previous_view: str = Field(default="", description="The view it was on before, empty on connect")
    active_desktop: DesktopId | None = Field(default=None, description="The desktop the client is on now")
    previous_desktop: str = Field(default="", description="The desktop it was on before, empty on connect")

    @model_validator(mode="after")
    def _require_a_view_or_a_desktop(self) -> "ClientStateReport":
        if self.active_view is None and self.active_desktop is None:
            raise InvalidShellValueError("a client_state report names an active_view or an active_desktop")
        return self


class ClientActivityReport(FrozenModel):
    """The body of ``POST /api/client-activity`` (desktop contracts.md section 6): a message a client sent."""

    client_id: ClientId = Field(description="The client the activity belongs to")
    desktop_id: DesktopId = Field(description="The desktop the client was on")
    kind: ClientActivityKind = Field(description="A message sent to an app's page")
    app: str = Field(description="The app the message went to")
    key: str = Field(description="The marker of the page the message went to (a chat id); empty for a page without one")
    text: str = Field(default="", description="The message text, truncated at write time")


class TabInstanceReport(FrozenModel):
    """The body of ``POST /api/tabs/<tab_id>/instance`` (contracts.md section 5)."""

    app: AppName = Field(description="The app that owns the tab's instance")
    key: str = Field(description="The key the tab now shows")


class LayoutSaveRequest(FrozenModel):
    """The body of ``POST /api/layouts/<view_id>`` (contracts.md section 6)."""

    client_id: ClientId = Field(description="The saving client")
    save_id: SaveId = Field(description="The save id the window minted, echoed in the layout_updated broadcast")
    base_updated_at: AwareDatetime | None = Field(
        default=None,
        description="The updated_at of the arrangement the window last fetched or saved; None for one it only saw empty",
    )
    device_kind: DeviceKind = Field(description="The device kind the arrangement was made on")
    dockview: dict[str, Any] | None = Field(description="The serialized dockview grid, its panels' params included")

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_tabs(cls, data: Any) -> Any:
        return fold_legacy_tabs_into_dockview(data)


class ClientReportOutcome(FrozenModel):
    """What recording a ``client_state`` report came to: the record, and whether its active view or desktop moved."""

    record: ClientRecord = Field(description="The client record as written")
    is_active_view_changed: bool = Field(description="Whether the stored active view differs from before the report")
    is_active_desktop_changed: bool = Field(
        default=False, description="Whether the stored active desktop differs from before the report"
    )


class LayoutEditOutcome(FrozenModel):
    """What editing a client's layout under the state lock came to: the arrangement now in force, and whether it was written."""

    layout: LayoutRecord = Field(description="The arrangement after the edit, stamped when it was written")
    is_written: bool = Field(
        description="Whether the edit changed the arrangement and was written to the client's file"
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
        "params": [str(param) for param in launch_path.params],
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
    is_settling: bool = Field(description="True from an open at a launch path until the page's first location report")


class Desktop(FrozenModel):
    """A named, shared collection of windows and shortcuts over a wallpaper (desktop contracts.md section 4.1)."""

    id: DesktopId = Field(description="The slugified name, stable across renames")
    name: str = Field(description="Free-form name shown in the UI")
    color: str = Field(description="Accent colour as a '#RRGGBB' string")
    glyph: int = Field(description="Index into the frontend's glyph table")
    sharing: SharingMode = Field(description="Shared or personal; stored and shown, enforced by nothing in V1")
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
    path: WindowPath = Field(description="The path under the app origin, with a query string for a launch path")
    client_id: ClientId = Field(description="The requesting client, whose placement is written at once")
    if_present: IfPresent = Field(
        default=IfPresent.FOCUS, description="Focus a window already at the path, or open another"
    )
    launch: LaunchPathId | None = Field(
        default=None, description="The launch path the path was built from, when it was"
    )


class WindowLocationReport(FrozenModel):
    """The body of ``POST /api/desktops/<id>/windows/<window_id>/location``."""

    path: WindowPath = Field(description="Where the page is now")
    title: WindowTitle = Field(description="What the page calls itself now")


class WindowOpenOutcome(FrozenModel):
    """What an open came to: the window answered, and whether it was opened rather than found."""

    window: Window = Field(description="The window opened or focused")
    is_new: bool = Field(description="True for an open, False when an existing window was answered")


class DesktopChangeOutcome(FrozenModel):
    """What a desktop write that may change nothing came to: the desktop, and whether it was written."""

    desktop: Desktop = Field(description="The desktop after the edit")
    is_written: bool = Field(description="Whether the edit changed the desktop and was written")


class PlacementsEditOutcome(FrozenModel):
    """What editing a client's layout of a desktop under the state lock came to."""

    layout: DesktopLayout = Field(description="The layout after the edit, stamped when it was written")
    is_written: bool = Field(description="Whether the edit changed the layout and was written")


class DesktopDeleteOutcome(FrozenModel):
    """What deleting a desktop came to: the desktop that went, and where its clients fall back to."""

    deleted: Desktop = Field(description="The desktop as it was")
    fallback_desktop_id: DesktopId = Field(description="The first remaining desktop")


@pure
def desktop_wire_json(desktop: Desktop) -> dict[str, Any]:
    """The ``desktop`` object of desktop contracts.md section 4.1."""
    return desktop.model_dump(mode="json")


@pure
def window_wire_json(window: Window) -> dict[str, Any]:
    """The ``window`` object of desktop contracts.md section 4.1."""
    return window.model_dump(mode="json")


@pure
def desktop_layout_wire_json(layout: DesktopLayout) -> dict[str, Any]:
    """The ``layout`` object of desktop contracts.md section 4.2."""
    return layout.model_dump(mode="json")
