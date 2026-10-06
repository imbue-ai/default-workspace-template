"""The desktop's routes (desktop-interface contracts.md sections 5 and 8): desktops, windows, placements, wallpapers,
and the verbs of the op route."""

from collections.abc import Callable
from collections.abc import Sequence
from typing import Any
from typing import Literal
from typing import assert_never

from app_manifest.manifest import ShortcutMode
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from flask import Flask
from flask import jsonify
from flask import request
from flask import send_file
from flask.typing import ResponseReturnValue
from loguru import logger
from pydantic import Field
from workspace_layout.answers import ClientView
from workspace_layout.answers import ContextAnswer
from workspace_layout.answers import DesktopOpAnswer
from workspace_layout.answers import DesktopsListing
from workspace_layout.answers import InventoryClient
from workspace_layout.answers import InventoryDocument
from workspace_layout.answers import InventoryOpAnswer
from workspace_layout.answers import LayoutOpMessage
from workspace_layout.answers import LayoutOpMessageArgs
from workspace_layout.answers import OpenAnswer
from workspace_layout.answers import PoppedOutWindow
from workspace_layout.answers import ShowAnswer
from workspace_layout.answers import TransientOpAnswer
from workspace_layout.ops import ContextBody
from workspace_layout.ops import InventoryBody
from workspace_layout.ops import LoadBody
from workspace_layout.ops import NavigateArgs
from workspace_layout.ops import NavigateBody
from workspace_layout.ops import OpBody
from workspace_layout.ops import OpRequester
from workspace_layout.ops import OpTarget
from workspace_layout.ops import OpenArgs
from workspace_layout.ops import OpenBody
from workspace_layout.ops import PlaceArgs
from workspace_layout.ops import PlaceBody
from workspace_layout.ops import RefreshAppBody
from workspace_layout.ops import RefreshWindowArgs
from workspace_layout.ops import RefreshWindowBody
from workspace_layout.ops import ReloadSystemInterfaceBody
from workspace_layout.ops import ShortcutMoveBody
from workspace_layout.ops import ShortcutRemoveBody
from workspace_layout.ops import ShortcutSetBody
from workspace_layout.ops import ShortcutsBody
from workspace_layout.ops import ShowArgs
from workspace_layout.ops import ShowBody
from workspace_layout.ops import WallpaperBody
from workspace_layout.ops import WindowArgs
from workspace_layout.ops import WindowOp
from workspace_layout.ops import WindowOpBody
from workspace_layout.ops import parse_window_reference
from workspace_layout.ops import requester_spelling
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import IfPresent
from workspace_layout.primitives import LayoutOp
from workspace_layout.primitives import ShowOutcome
from workspace_layout.primitives import SpecialWindow
from workspace_layout.primitives import WallpaperKind
from workspace_layout.primitives import WallpaperName
from workspace_layout.primitives import WindowId
from workspace_layout.primitives import WindowPath
from workspace_layout.primitives import WindowState
from workspace_layout.records import ClientRecord
from workspace_layout.records import Desktop
from workspace_layout.records import DesktopLayout
from workspace_layout.records import DesktopLayoutView
from workspace_layout.records import DesktopShortcut
from workspace_layout.records import Frame
from workspace_layout.records import GridCell
from workspace_layout.records import ShortcutTarget
from workspace_layout.records import Wallpaper
from workspace_layout.records import Window
from workspace_layout.shell_url import DESKTOPS_ROUTE

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.system_interface.app_context import get_state
from imbue.system_interface.shell.client_activity import summarize_client_activity
from imbue.system_interface.shell.clients import client_view
from imbue.system_interface.shell.data_types import ClientArrivalOutcome
from imbue.system_interface.shell.data_types import ClientDesktopView
from imbue.system_interface.shell.data_types import LaunchRequest
from imbue.system_interface.shell.data_types import PlacementsSaveRequest
from imbue.system_interface.shell.data_types import WindowLocationReport
from imbue.system_interface.shell.data_types import WindowOpenRequest
from imbue.system_interface.shell.data_types import desktop_layout_view
from imbue.system_interface.shell.data_types import effective_launch_paths
from imbue.system_interface.shell.desktop_document import choose_show_target
from imbue.system_interface.shell.desktop_document import default_launch_path_id
from imbue.system_interface.shell.desktop_document import effective_placements
from imbue.system_interface.shell.desktop_document import find_window_at
from imbue.system_interface.shell.desktop_document import frame_for_state
from imbue.system_interface.shell.desktop_document import most_recently_focused_window_of_app
from imbue.system_interface.shell.desktop_document import next_shortcut_cell
from imbue.system_interface.shell.desktop_document import paired_frames
from imbue.system_interface.shell.desktop_document import path_carries_marker
from imbue.system_interface.shell.desktop_document import placement_of
from imbue.system_interface.shell.desktop_document import require_window
from imbue.system_interface.shell.desktop_document import with_window_frame
from imbue.system_interface.shell.desktop_document import with_window_minimized
from imbue.system_interface.shell.desktop_document import with_window_minimized_on_desktop
from imbue.system_interface.shell.desktop_document import with_window_raised
from imbue.system_interface.shell.desktop_document import with_window_restored
from imbue.system_interface.shell.desktop_document import with_window_state
from imbue.system_interface.shell.desktops import find_desktop_by_name_or_id
from imbue.system_interface.shell.desktops import resolve_active_desktop
from imbue.system_interface.shell.errors import DesktopNotFoundError
from imbue.system_interface.shell.errors import LayoutOpError
from imbue.system_interface.shell.errors import NoRequesterWindowError
from imbue.system_interface.shell.errors import WallpaperNotFoundError
from imbue.system_interface.shell.errors import WindowNotFoundError
from imbue.system_interface.shell.errors import WindowPoppedOutError
from imbue.system_interface.shell.route_helpers import HTTP_CREATED
from imbue.system_interface.shell.route_helpers import HTTP_NOT_FOUND
from imbue.system_interface.shell.route_helpers import HTTP_NO_CONTENT
from imbue.system_interface.shell.route_helpers import HTTP_OK
from imbue.system_interface.shell.route_helpers import detail_response
from imbue.system_interface.shell.route_helpers import parse_request_body
from imbue.system_interface.shell.route_helpers import request_identity
from imbue.system_interface.shell.route_helpers import require_client
from imbue.system_interface.shell.route_helpers import resolve_client
from imbue.system_interface.shell.state import ShellState
from imbue.system_interface.shell.wallpapers import BUNDLED_WALLPAPERS_DIRNAME
from imbue.system_interface.shell.wallpapers import WALLPAPER_ROUTE_PREFIX
from imbue.system_interface.shell.wallpapers import WallpaperDirectories
from imbue.system_interface.shell.wallpapers import list_wallpapers
from imbue.system_interface.shell.wallpapers import resolve_wallpaper_file
from imbue.system_interface.shell.wallpapers import wallpaper_listing_wire_json


class DesktopMetadataRequest(FrozenModel):
    """The body of desktop create."""

    name: str = Field(description="The display name")
    color: str = Field(description="'#RRGGBB'")
    glyph: int = Field(description="The glyph index")


class DesktopSettingsRequest(FrozenModel):
    """The body of desktop settings."""

    name: str = Field(description="The display name")
    color: str = Field(description="'#RRGGBB'")
    glyph: int = Field(description="The glyph index")


class DesktopWallpaperRequest(FrozenModel):
    """The body of the wallpaper route."""

    wallpaper: Wallpaper | None = Field(description="The wallpaper reference, or null for the theme's default")


class DesktopShortcutRequest(FrozenModel):
    """The body of the shortcut set route."""

    target: ShortcutTarget = Field(description="The launch path the shortcut runs")
    mode: ShortcutMode = Field(description="focus or new")
    cell: GridCell = Field(description="The cell it sits in")


class ShortcutMoveRequest(FrozenModel):
    """The body of the shortcut move route."""

    app: AppName = Field(description="The app")
    launch: LaunchPathId = Field(description="The launch path id")
    cell: GridCell = Field(description="The cell to move to")


class ShortcutRemoveRequest(FrozenModel):
    """The body of the shortcut remove route."""

    app: AppName = Field(description="The app")
    launch: LaunchPathId = Field(description="The launch path id")


def _shell() -> ShellState:
    return get_state().shell


def _wallpaper_directories() -> WallpaperDirectories:
    return WallpaperDirectories(
        bundled=get_state().static_directory / BUNDLED_WALLPAPERS_DIRNAME, files=_shell().wallpaper_files_directory
    )


def _validated_target(shell: ShellState, target: ShortcutTarget) -> ShortcutTarget:
    """A shortcut target whose launch path the app declares (or the synthesized ``open``); a 400 otherwise."""
    shell.require_launch_path(shell.require_app_entry(str(target.app)), target.launch)
    return target


def _existing_wallpaper(wallpaper: Wallpaper | None) -> Wallpaper | None:
    """The wallpaper reference when an image file backs it (or None for the default); a 404 otherwise."""
    if wallpaper is not None and resolve_wallpaper_file(wallpaper, _wallpaper_directories()) is None:
        raise WallpaperNotFoundError(f"No {wallpaper.kind.value} wallpaper named {str(wallpaper.name)!r}")
    return wallpaper


# Section 5.2: desktops


def list_desktops() -> ResponseReturnValue:
    shell = _shell()
    return jsonify(DesktopsListing(desktops=tuple(shell.desktop_views(shell.list_desktops()))).model_dump(mode="json"))


def create_desktop() -> ResponseReturnValue:
    body = parse_request_body(DesktopMetadataRequest)
    shell = _shell()
    desktop = shell.create_desktop(body.name, body.color, body.glyph)
    return jsonify(shell.desktop_view(desktop).model_dump(mode="json")), HTTP_CREATED


def update_desktop_settings(desktop_id: str) -> ResponseReturnValue:
    body = parse_request_body(DesktopSettingsRequest)
    shell = _shell()
    desktop = shell.desktops.update_settings(desktop_id, body.name, body.color, body.glyph)
    shell.broadcast_desktops_updated()
    return jsonify(shell.desktop_view(desktop).model_dump(mode="json"))


def set_desktop_wallpaper(desktop_id: str) -> ResponseReturnValue:
    body = parse_request_body(DesktopWallpaperRequest)
    shell = _shell()
    desktop = shell.desktops.set_wallpaper(desktop_id, _existing_wallpaper(body.wallpaper))
    shell.broadcast_desktops_updated()
    return jsonify(shell.desktop_view(desktop).model_dump(mode="json"))


def delete_desktop(desktop_id: str) -> ResponseReturnValue:
    outcome = _shell().delete_desktop(desktop_id)
    return jsonify({"fallback_desktop_id": str(outcome.fallback_desktop_id)})


def set_desktop_shortcut(desktop_id: str) -> ResponseReturnValue:
    body = parse_request_body(DesktopShortcutRequest)
    shell = _shell()
    shortcut = DesktopShortcut(target=_validated_target(shell, body.target), mode=body.mode, cell=body.cell)
    desktop = shell.desktops.set_shortcut(desktop_id, shortcut)
    shell.broadcast_desktops_updated()
    return jsonify(shell.desktop_view(desktop).model_dump(mode="json"))


def move_desktop_shortcut(desktop_id: str) -> ResponseReturnValue:
    body = parse_request_body(ShortcutMoveRequest)
    shell = _shell()
    desktop = shell.desktops.move_shortcut(desktop_id, body.app, body.launch, body.cell)
    shell.broadcast_desktops_updated()
    return jsonify(shell.desktop_view(desktop).model_dump(mode="json"))


def remove_desktop_shortcut(desktop_id: str) -> ResponseReturnValue:
    body = parse_request_body(ShortcutRemoveRequest)
    shell = _shell()
    desktop = shell.desktops.remove_shortcut(desktop_id, body.app, body.launch)
    shell.broadcast_desktops_updated()
    return jsonify(shell.desktop_view(desktop).model_dump(mode="json"))


# Section 5.3: windows


def open_window(desktop_id: str) -> ResponseReturnValue:
    body = parse_request_body(WindowOpenRequest)
    outcome = _shell().open_window(desktop_id, body)
    return (
        jsonify({"window": _shell().window_view(outcome.window).model_dump(mode="json"), "is_new": outcome.is_new}),
        HTTP_CREATED if outcome.is_new else HTTP_OK,
    )


def close_window(desktop_id: str, window_id: str) -> ResponseReturnValue:
    _shell().close_window(desktop_id, WindowId(window_id))
    return "", HTTP_NO_CONTENT


def report_window_location(desktop_id: str, window_id: str) -> ResponseReturnValue:
    body = parse_request_body(WindowLocationReport)
    window = _shell().report_window_location(desktop_id, WindowId(window_id), body.client_id, body.path, body.title)
    return jsonify(_shell().window_view(window).model_dump(mode="json"))


def launch(desktop_id: str) -> ResponseReturnValue:
    """``POST /api/desktops/<id>/launch`` (post-launch-paths plan section 5.3): run a launch path for a client and
    answer the window showing its page, the page's path, and whether the window was opened."""
    body = parse_request_body(LaunchRequest)
    outcome = _shell().launch(desktop_id, body)
    return (
        jsonify(
            {
                "window": _shell().window_view(outcome.window).model_dump(mode="json"),
                "path": str(outcome.path),
                "is_new": outcome.is_new,
            }
        ),
        HTTP_CREATED if outcome.is_new else HTTP_OK,
    )


# Section 5.4: placements


def _layout_view(shell: ShellState, desktop: Desktop, client_id: ClientId) -> DesktopLayoutView:
    """The client's layout of the desktop with its stored paths for the desktop's independent windows."""
    return desktop_layout_view(
        shell.read_desktop_layout(desktop, client_id), shell.read_window_paths(desktop, client_id)
    )


def get_placements(desktop_id: str) -> ResponseReturnValue:
    client_id = ClientId(request.args.get("client", ""))
    shell = _shell()
    return jsonify(_layout_view(shell, shell.get_desktop(desktop_id), client_id).model_dump(mode="json"))


def save_placements(desktop_id: str) -> ResponseReturnValue:
    body = parse_request_body(PlacementsSaveRequest)
    saved = _shell().save_browser_placements(desktop_id, body)
    # The stamp is spelled as the read route spells it, so the window compares like with like.
    return jsonify(
        {
            "updated_at": desktop_layout_view(saved, {}).model_dump(mode="json")["updated_at"]
            if saved is not None
            else None
        }
    )


# Section 5.5: the arrival, wallpapers, and the inventory document


def arrival_wire_json(shell: ShellState, outcome: ClientArrivalOutcome | None) -> dict[str, Any]:
    """The answer of ``POST /api/clients/<client_id>/arrive`` (desktop contracts.md section 5.5); None (no desktop
    yet) answers three nulls."""
    if outcome is None:
        return {"desktop_id": None, "created_desktop": None, "replaced_desktop_name": None}
    return {
        "desktop_id": str(outcome.desktop_id),
        "created_desktop": (
            shell.desktop_view(outcome.created_desktop).model_dump(mode="json")
            if outcome.created_desktop is not None
            else None
        ),
        "replaced_desktop_name": outcome.replaced_desktop_name,
    }


def arrive_client(client_id: str) -> ResponseReturnValue:
    """A shell page has loaded for ``client_id``: settle the desktop it lands on from the requester's identity."""
    shell = _shell()
    return jsonify(arrival_wire_json(shell, shell.arrive_client(ClientId(client_id), request_identity())))


def list_wallpapers_route() -> ResponseReturnValue:
    return jsonify(
        {"wallpapers": [wallpaper_listing_wire_json(listing) for listing in list_wallpapers(_wallpaper_directories())]}
    )


def serve_wallpaper(kind: str, name: str) -> ResponseReturnValue:
    try:
        wallpaper = Wallpaper(kind=WallpaperKind(kind), name=WallpaperName(name))
    except ValueError as e:
        logger.debug("Refused a wallpaper request for kind {!r} and name {!r}: {}", kind, name, e)
        return detail_response(f"No wallpaper of kind {kind!r} named {name!r}", HTTP_NOT_FOUND)
    path = resolve_wallpaper_file(wallpaper, _wallpaper_directories())
    if path is None:
        return detail_response(f"No {wallpaper.kind.value} wallpaper named {str(wallpaper.name)!r}", HTTP_NOT_FOUND)
    return send_file(path)


@pure
def _shown_window_ids(desktop: Desktop, layout: DesktopLayout) -> tuple[WindowId, ...]:
    return tuple(
        placement.window_id for placement in effective_placements(layout, desktop) if not placement.is_minimized
    )


@pure
def _with_active_desktop(record: ClientRecord, active: DesktopId | None) -> ClientRecord:
    return record.model_copy_update(to_update(record.field_ref().active_desktop, active))


@pure
def resolved_client_view(record: ClientRecord, is_connected: bool, desktops: Sequence[Desktop]) -> ClientView:
    """The ``client`` object of desktop contracts.md section 5.5 with its desktop settled by the rule of section 4.3:
    the stored one when a desktop of that id exists, else the first desktop (a stored id nothing holds any more,
    or a view a version-1 clients file recorded, never reaches a reader)."""
    return client_view(_with_active_desktop(record, resolve_active_desktop(record, desktops)), is_connected)


@pure
def _popped_out_windows(desktop: Desktop, layout: DesktopLayout) -> tuple[PoppedOutWindow, ...]:
    """The client's popped-out windows of one desktop, each with whether the user hid its ghost."""
    return tuple(
        PoppedOutWindow(window_id=placement.window_id, desktop_id=desktop.id, is_ghost_hidden=placement.is_minimized)
        for placement in effective_placements(layout, desktop)
        if placement.is_detached
    )


def inventory_document(shell: ShellState) -> InventoryDocument:
    """The one document of desktop contracts.md section 5.5: whether a preview shell answered, the workspace's name,
    every desktop, every app, and every known client with ``shown``, the windows of its active desktop that its layout
    does not minimize, and ``popped_out``, the windows its layouts of every desktop say it popped out into their own
    windows."""
    desktops = shell.list_desktops()
    connected = shell.broadcaster.connected_client_ids()
    clients: list[InventoryClient] = []
    for record in shell.clients.list_clients():
        active = resolve_active_desktop(record, desktops)
        shown: tuple[WindowId, ...] = ()
        popped_out: list[PoppedOutWindow] = []
        for desktop in desktops:
            layout = shell.placements.read_layout(
                desktop.id, str(record.id), {window.id for window in desktop.windows}
            )
            if desktop.id == active:
                shown = _shown_window_ids(desktop, layout)
            popped_out.extend(_popped_out_windows(desktop, layout))
        view = client_view(_with_active_desktop(record, active), str(record.id) in connected)
        clients.append(InventoryClient.model_validate({**dict(view), "shown": shown, "popped_out": tuple(popped_out)}))
    return InventoryDocument(
        is_preview=get_state().is_preview,
        workspace_name=get_state().workspace_name.resolve(),
        desktops=tuple(shell.desktop_views(desktops)),
        apps=tuple(shell.inventory.views()),
        clients=tuple(clients),
    )


def register_desktop_routes(application: Flask) -> None:
    """Register the desktop interface's routes on ``application`` (the error handlers are the shell's)."""
    application.add_url_rule(DESKTOPS_ROUTE, view_func=list_desktops, methods=["GET"], endpoint="list_desktops")
    application.add_url_rule(DESKTOPS_ROUTE, view_func=create_desktop, methods=["POST"], endpoint="create_desktop")
    application.add_url_rule(
        "/api/desktops/<desktop_id>/settings",
        view_func=update_desktop_settings,
        methods=["POST"],
        endpoint="update_desktop_settings",
    )
    application.add_url_rule(
        "/api/desktops/<desktop_id>/wallpaper",
        view_func=set_desktop_wallpaper,
        methods=["POST"],
        endpoint="set_desktop_wallpaper",
    )
    application.add_url_rule(
        "/api/desktops/<desktop_id>/delete", view_func=delete_desktop, methods=["POST"], endpoint="delete_desktop"
    )
    application.add_url_rule(
        "/api/desktops/<desktop_id>/shortcuts",
        view_func=set_desktop_shortcut,
        methods=["POST"],
        endpoint="set_desktop_shortcut",
    )
    application.add_url_rule(
        "/api/desktops/<desktop_id>/shortcuts/move",
        view_func=move_desktop_shortcut,
        methods=["POST"],
        endpoint="move_desktop_shortcut",
    )
    application.add_url_rule(
        "/api/desktops/<desktop_id>/shortcuts/remove",
        view_func=remove_desktop_shortcut,
        methods=["POST"],
        endpoint="remove_desktop_shortcut",
    )
    application.add_url_rule(
        "/api/desktops/<desktop_id>/windows", view_func=open_window, methods=["POST"], endpoint="open_window"
    )
    application.add_url_rule(
        "/api/desktops/<desktop_id>/launch", view_func=launch, methods=["POST"], endpoint="launch"
    )
    application.add_url_rule(
        "/api/desktops/<desktop_id>/windows/<window_id>/close",
        view_func=close_window,
        methods=["POST"],
        endpoint="close_window",
    )
    application.add_url_rule(
        "/api/desktops/<desktop_id>/windows/<window_id>/location",
        view_func=report_window_location,
        methods=["POST"],
        endpoint="report_window_location",
    )
    application.add_url_rule(
        "/api/placements/<desktop_id>", view_func=get_placements, methods=["GET"], endpoint="get_placements"
    )
    application.add_url_rule(
        "/api/placements/<desktop_id>", view_func=save_placements, methods=["POST"], endpoint="save_placements"
    )
    application.add_url_rule(
        "/api/clients/<client_id>/arrive", view_func=arrive_client, methods=["POST"], endpoint="arrive_client"
    )
    application.add_url_rule(
        "/api/wallpapers", view_func=list_wallpapers_route, methods=["GET"], endpoint="list_wallpapers_route"
    )
    application.add_url_rule(
        f"{WALLPAPER_ROUTE_PREFIX}/<kind>/<name>",
        view_func=serve_wallpaper,
        methods=["GET"],
        endpoint="serve_wallpaper",
    )


# Section 8: the desktop verbs of the op route


class _DesktopOpTarget(FrozenModel):
    """What a desktop op acts on once its target is settled: the client, and the desktop it edits."""

    client_id: ClientId = Field(description="The one client the op targets")
    desktop: Desktop = Field(description="The desktop the op edits (the client's active one, or ``args.desktop``)")
    is_switch_asked: bool = Field(
        default=False,
        description="Whether ``args.desktop`` names a desktop other than the client's active one, which the op "
        "switches the client to once it has been applied",
    )


class _PopOutNotes(FrozenModel):
    """What an op's answer says about the target client's popped-out windows (plan-popped-out-layout-ops.md)."""

    is_raised_in_own_window: bool = Field(
        default=False, description="The window was popped out, so its own window was raised and it stayed out"
    )
    is_brought_back: bool = Field(
        default=False,
        description="The op brought a pulled-out window back onto the desktop (forced, or for a client that is not "
        "connected)",
    )
    unpaired_beside: WindowId | None = Field(
        default=None, description="The popped-out window an ``open --beside`` named, which the open did not pair with"
    )
    has_no_desktop_window: bool = Field(
        default=False,
        description="The client's only open windows are pop-outs, so the window the op shows on the desktop is "
        "stored for when a desktop window opens",
    )


def _resolve_target(shell: ShellState, arguments: OpTarget, requester: OpRequester | None) -> _DesktopOpTarget:
    """The client and desktop an op targets: ``args.desktop`` (by name or id), else the client's active desktop by the
    rule of desktop contracts.md section 4.3. The switch ``args.desktop`` asks for is left to ``_switch_as_asked``,
    once the op has been applied, so an op refused on the way leaves the client where it was."""
    client_id = require_client(shell, arguments.client, requester)
    desktops = shell.list_desktops()
    if arguments.desktop is not None:
        desktop = find_desktop_by_name_or_id(desktops, arguments.desktop)
        if desktop is None:
            raise DesktopNotFoundError(arguments.desktop)
        is_switch_asked = shell.active_desktop_of_client(client_id) != desktop.id
        return _DesktopOpTarget(client_id=client_id, desktop=desktop, is_switch_asked=is_switch_asked)
    active = shell.active_desktop_of_client(client_id)
    if active is None:
        raise LayoutOpError("there is no desktop to edit yet")
    return _DesktopOpTarget(client_id=client_id, desktop=shell.get_desktop(active))


def _switch_as_asked(shell: ShellState, target: _DesktopOpTarget) -> None:
    """Switch the client to the desktop ``args.desktop`` named, when it named another than the client's active one."""
    if target.is_switch_asked:
        shell.set_client_active_desktop(target.client_id, target.desktop.id)


def _is_detached_while_connected(shell: ShellState, client_id: ClientId, is_detached: bool) -> bool:
    """Whether the client has a window popped out into its own window: its own placement is detached, and the client
    is connected. A disconnected client's placement is only a record, which the pop-out reopened at its next launch
    reconciles with whatever an op made of it."""
    return is_detached and str(client_id) in shell.broadcaster.connected_client_ids()


def _has_no_desktop_window(shell: ShellState, client_id: ClientId) -> bool:
    """Whether the client is connected only through pop-outs: a window shown on its desktop waits for a desktop
    window to open."""
    client = str(client_id)
    return client in shell.broadcaster.connected_client_ids() and client not in (
        shell.broadcaster.desktop_connected_client_ids()
    )


def _raise_in_own_window(
    shell: ShellState, window_id: WindowId, client_id: ClientId, requester: OpRequester | None
) -> None:
    """Ask the client's windows to raise a popped-out window's own desktop window, leaving its placement as it stands:
    only the client's pages can bring that window forward, and raising the placement would pull the window back onto
    the desktop instead."""
    _announce_window_op(shell, LayoutOp.SHOW, window_id, client_id, requester, is_detached=True)


def _resolve_window(
    desktop: Desktop,
    seen_windows: Sequence[Window],
    layout: DesktopLayout,
    raw: str,
    requester: OpRequester | None,
) -> Window:
    """The window an op names, as the shared record: a window id, ``self`` (the requester's app's window whose path,
    as the target client sees it in ``seen_windows``, carries its marker), ``pinned`` (the requester's app's pinned
    window on the desktop), or an app name (that app's most recently focused window in this client's layout)."""
    reference = parse_window_reference(raw)
    match reference:
        case SpecialWindow.PINNED:
            if requester is None:
                raise NoRequesterWindowError(
                    "'pinned' names the requester's app's pinned window, but this op carried no requester"
                )
            pinned = next(
                (window for window in desktop.windows if window.is_pinned and window.app == requester.app), None
            )
            if pinned is None:
                raise WindowNotFoundError(f"pinned ({requester.app} has no pinned window on desktop {desktop.id})")
            return pinned
        case SpecialWindow.SELF:
            if requester is None or not requester.marker:
                raise NoRequesterWindowError(
                    "'self' names the requester's own window, but this op carried no requester with a marker"
                )
            own = next(
                (
                    window
                    for window in seen_windows
                    if window.app == requester.app and path_carries_marker(window.path, requester.marker)
                ),
                None,
            )
            if own is None:
                raise WindowNotFoundError(
                    f"self ({requester.app} carrying {requester.marker!r} on desktop {desktop.id})"
                )
            return require_window(desktop, own.id)
        case WindowId():
            return require_window(desktop, reference)
        case AppName():
            window = most_recently_focused_window_of_app(layout, desktop, reference)
            if window is None:
                raise WindowNotFoundError(f"{reference} (no window of it on desktop {desktop.id})")
            return window
        case _:
            assert_never(reference)


class _OpenTarget(FrozenModel):
    """What an ``open`` op opens: the app and the page path, resolved from an explicit path or a launch path."""

    app: AppName = Field(description="The app")
    path: WindowPath = Field(description="The explicit path, or the page the launch path resolved to")


def _open_target(
    shell: ShellState, arguments: OpenArgs, client_id: ClientId | None, desktop_id: DesktopId | None
) -> _OpenTarget:
    """What an ``open`` op opens: an explicit path, else the page of the launch path it names (the app's default, or
    its first), resolved as the launch route resolves one (a GET launch path with its params as the query, a POST one
    asked for its page with the client and its desktop as the envelope, or no envelope for an open with no client)."""
    entry = shell.require_app_entry(str(arguments.app))
    if arguments.path is not None:
        return _OpenTarget(app=arguments.app, path=arguments.path)
    default_launch = default_launch_path_id(entry.row)
    offered = effective_launch_paths(entry.row)
    launch_id = (
        arguments.launch
        if arguments.launch is not None
        else (default_launch if default_launch is not None else offered[0].id)
    )
    launch_path = shell.require_launch_path(entry, launch_id)
    return _OpenTarget(
        app=arguments.app,
        path=shell.launch_destination(entry, launch_path, arguments.params, client_id, desktop_id, None),
    )


def _open_request(
    shell: ShellState, arguments: OpenArgs, client_id: ClientId, desktop_id: DesktopId
) -> WindowOpenRequest:
    target = _open_target(shell, arguments, client_id, desktop_id)
    return WindowOpenRequest(
        app=target.app,
        path=target.path,
        client_id=client_id,
        if_present=arguments.if_present,
        minimized=arguments.minimized,
    )


def _open_unplaced(shell: ShellState, arguments: OpenArgs, requester: OpRequester | None) -> ResponseReturnValue:
    """An ``open`` with no client to target (desktop contracts.md section 8): the window is written on the named
    desktop, else the first, with no placement, so it shows minimized for every client rather than being refused."""
    desktops = shell.list_desktops()
    if arguments.desktop is not None:
        desktop = find_desktop_by_name_or_id(desktops, arguments.desktop)
        if desktop is None:
            raise DesktopNotFoundError(arguments.desktop)
    elif desktops:
        desktop = desktops[0]
    else:
        raise LayoutOpError("there is no desktop to open on yet")
    target = _open_target(shell, arguments, None, None)
    outcome = shell.open_window_unplaced(desktop.id, target.app, target.path, arguments.if_present)
    logger.info(
        "layout op=open requester={} desktop={} client=none args={}", requester, desktop.id, _logged(arguments)
    )
    answer = OpenAnswer(
        desktop_id=desktop.id,
        client_id=None,
        desktop=shell.desktop_view(shell.get_desktop(desktop.id)),
        layout=None,
        window_id=outcome.window.id,
    )
    return jsonify(answer.model_dump(mode="json"))


def _answer_document(
    shell: ShellState, target: _DesktopOpTarget, window_id: WindowId | None, notes: _PopOutNotes
) -> DesktopOpAnswer:
    desktop = shell.get_desktop(target.desktop.id)
    return DesktopOpAnswer(
        desktop_id=desktop.id,
        client_id=target.client_id,
        desktop=shell.desktop_view(desktop),
        layout=_layout_view(shell, desktop, target.client_id),
        window_id=window_id,
        is_raised_in_own_window=notes.is_raised_in_own_window,
        is_brought_back=notes.is_brought_back,
        unpaired_beside=notes.unpaired_beside,
        has_no_desktop_window=notes.has_no_desktop_window,
    )


@pure
def _logged(arguments: FrozenModel) -> dict[str, Any]:
    """An op's arguments as the log line records them: the ones the caller sent."""
    return arguments.model_dump(mode="json", exclude_unset=True)


def _applied(
    shell: ShellState, body: OpBody, target: _DesktopOpTarget, window_id: WindowId | None, notes: _PopOutNotes
) -> ResponseReturnValue:
    """Answer a document op the shell has applied, with the desktop and the target client's layout of it."""
    logger.info(
        "layout op={} requester={} desktop={} client={} args={}",
        body.op,
        body.requester,
        target.desktop.id,
        target.client_id,
        _logged(body.args),
    )
    return jsonify(_answer_document(shell, target, window_id, notes).model_dump(mode="json"))


_ShortcutBody = ShortcutsBody | ShortcutSetBody | ShortcutMoveBody | ShortcutRemoveBody | WallpaperBody


def _op_shortcuts(shell: ShellState, body: _ShortcutBody, target: _DesktopOpTarget) -> None:
    desktop_id = target.desktop.id
    match body:
        case ShortcutsBody():
            return
        case ShortcutSetBody(args=arguments):
            shortcut_target = _validated_target(shell, ShortcutTarget(app=arguments.app, launch=arguments.launch))
            cell = arguments.cell if arguments.cell is not None else next_shortcut_cell(target.desktop)
            shell.desktops.set_shortcut(
                desktop_id, DesktopShortcut(target=shortcut_target, mode=arguments.mode, cell=cell)
            )
        case ShortcutMoveBody(args=arguments):
            shell.desktops.move_shortcut(desktop_id, arguments.app, arguments.launch, arguments.cell)
        case ShortcutRemoveBody(args=arguments):
            shell.desktops.remove_shortcut(desktop_id, arguments.app, arguments.launch)
        case WallpaperBody(args=arguments):
            shell.desktops.set_wallpaper(desktop_id, _existing_wallpaper(arguments.wallpaper))
        case _:
            assert_never(body)
    shell.broadcast_desktops_updated()


class _NamedWindow(FrozenModel):
    """The window an op names on its target, and the desktop's windows as the target client sees them."""

    window: Window = Field(description="The window, as the shared record")
    seen_windows: tuple[Window, ...] = Field(description="The desktop's windows as the target client sees them")


def _named_window(
    shell: ShellState, target: _DesktopOpTarget, raw: str, requester: OpRequester | None
) -> _NamedWindow:
    desktop = target.desktop
    layout = shell.read_desktop_layout(desktop, target.client_id)
    seen_windows = shell.windows_for_client(desktop, target.client_id)
    window = _resolve_window(desktop, seen_windows, layout, raw, requester)
    return _NamedWindow(window=window, seen_windows=seen_windows)


_LayoutEdit = Callable[[DesktopLayout], DesktopLayout]


def _is_detached(shell: ShellState, target: _DesktopOpTarget, window_id: WindowId) -> bool:
    return placement_of(shell.read_desktop_layout(target.desktop, target.client_id), window_id).is_detached


def _move_placement(
    shell: ShellState,
    op: LayoutOp,
    target: _DesktopOpTarget,
    window_id: WindowId,
    is_detached: bool,
    edit: _LayoutEdit,
    is_forced: bool,
) -> _PopOutNotes:
    """Write an edit that changes where a window sits, refusing it for a window the client has popped out unless the
    op is forced. A pulled-out window the edit does move (forced, or the client's while it is not connected) comes
    back onto the desktop, and the answer says so."""
    if _is_detached_while_connected(shell, target.client_id, is_detached) and not is_forced:
        raise WindowPoppedOutError(str(op), str(window_id))
    shell.edit_desktop_layout(target.desktop, target.client_id, edit)
    return _PopOutNotes(is_brought_back=is_detached)


@pure
def _placement_edit(op: LayoutOp, window_id: WindowId, is_detached: bool) -> _LayoutEdit:
    """The edit ``minimize``, ``restore``, or ``maximize`` makes. A pulled-out window a ``minimize`` takes out of
    sight comes back onto the desktop minimized, since minimizing it where it stands would only hide its ghost and
    leave its own window up."""
    match op:
        case LayoutOp.MINIMIZE if is_detached:
            return lambda current: with_window_minimized_on_desktop(current, window_id)
        case LayoutOp.MINIMIZE:
            return lambda current: with_window_minimized(current, window_id)
        case LayoutOp.RESTORE:
            return lambda current: with_window_restored(current, window_id)
        case LayoutOp.MAXIMIZE:
            return lambda current: with_window_state(current, window_id, WindowState.MAXIMIZED)
        case _:
            raise LayoutOpError(f"Op {op!r} sets no placement")


def _op_window(
    shell: ShellState, op: WindowOp, arguments: WindowArgs, target: _DesktopOpTarget, requester: OpRequester | None
) -> tuple[WindowId, _PopOutNotes]:
    """Apply one of the window verbs, refusing to move a window the client has popped out unless the op is forced
    (``focus`` raises the pop-out instead, and ``close`` never asks)."""
    desktop = target.desktop
    window = _named_window(shell, target, arguments.window, requester).window
    is_detached = _is_detached(shell, target, window.id)
    match op:
        case LayoutOp.FOCUS:
            if _is_detached_while_connected(shell, target.client_id, is_detached):
                _raise_in_own_window(shell, window.id, target.client_id, requester)
                return window.id, _PopOutNotes(is_raised_in_own_window=True)
            shell.edit_desktop_layout(
                desktop, target.client_id, lambda current: with_window_raised(current, window.id)
            )
            return window.id, _PopOutNotes(is_brought_back=is_detached)
        case LayoutOp.MINIMIZE | LayoutOp.RESTORE | LayoutOp.MAXIMIZE:
            edit = _placement_edit(op, window.id, is_detached)
            return window.id, _move_placement(shell, op, target, window.id, is_detached, edit, arguments.force)
        case LayoutOp.CLOSE:
            if window.is_pinned:
                raise LayoutOpError(f"window {window.id} is pinned and cannot be closed; minimize it instead")
            shell.close_window(desktop.id, window.id)
            return window.id, _PopOutNotes()
        case _:
            assert_never(op)


@pure
def _place_edit(arguments: PlaceArgs, window_id: WindowId) -> _LayoutEdit:
    state = arguments.state
    frame = arguments.frame
    if state is not None:
        return lambda current: with_window_state(current, window_id, state)
    if frame is not None:
        return lambda current: with_window_frame(current, window_id, frame)
    raise LayoutOpError("place takes a state or a frame")


def _place(
    shell: ShellState, arguments: PlaceArgs, target: _DesktopOpTarget, requester: OpRequester | None
) -> tuple[WindowId, _PopOutNotes]:
    window = _named_window(shell, target, arguments.window, requester).window
    edit = _place_edit(arguments, window.id)
    is_detached = _is_detached(shell, target, window.id)
    return window.id, _move_placement(shell, LayoutOp.PLACE, target, window.id, is_detached, edit, arguments.force)


def _after_window_op(
    shell: ShellState,
    op: WindowOp | Literal[LayoutOp.PLACE],
    target: _DesktopOpTarget,
    window_id: WindowId,
    notes: _PopOutNotes,
    requester: OpRequester | None,
) -> _PopOutNotes:
    """Finish a window verb once its edit is written: switch the client as ``args.desktop`` asked, announce a focus,
    and say whether the window waits for a desktop window. A window raised in its own window, or brought back from
    it, stays on its own desktop: the client's desktop window keeps the desktop it shows."""
    if not notes.is_raised_in_own_window and not notes.is_brought_back:
        _switch_as_asked(shell, target)
    if notes.is_raised_in_own_window:
        return notes
    match op:
        case LayoutOp.MINIMIZE | LayoutOp.CLOSE:
            return notes
        case LayoutOp.FOCUS | LayoutOp.RESTORE | LayoutOp.MAXIMIZE | LayoutOp.PLACE:
            # After the switch, so a phone looks the window up among the desktop it is moved to.
            if op is LayoutOp.FOCUS:
                _announce_window_op(shell, LayoutOp.FOCUS, window_id, target.client_id, requester)
            return notes.model_copy_update(
                to_update(notes.field_ref().has_no_desktop_window, _has_no_desktop_window(shell, target.client_id))
            )
        case _:
            assert_never(op)


def _navigate(
    shell: ShellState, arguments: NavigateArgs, target: _DesktopOpTarget, requester: OpRequester | None
) -> WindowId:
    named = _named_window(shell, target, arguments.window, requester)
    # As if the target client's page had reported it: an independent window moves for that client alone.
    seen = next(candidate for candidate in named.seen_windows if candidate.id == named.window.id)
    shell.report_window_location(target.desktop.id, named.window.id, target.client_id, arguments.path, seen.title)
    return named.window.id


def _client_desktop_view(shell: ShellState, desktop: Desktop, client_id: ClientId) -> ClientDesktopView:
    return ClientDesktopView(
        desktop=desktop,
        layout=shell.read_desktop_layout(desktop, client_id),
        seen_windows=shell.windows_for_client(desktop, client_id),
    )


def _show(
    shell: ShellState, arguments: ShowArgs, target: _DesktopOpTarget, requester: OpRequester | None
) -> ResponseReturnValue:
    """The ``show`` op: put the path of the app on the target client's screen, choosing the window by the rule
    ``choose_show_target`` spells, and answer which way it went."""
    app = arguments.app
    shell.require_app_entry(str(app))
    path = arguments.path
    showing = set(arguments.showing)
    repoint = set(arguments.repoint)
    client_id = target.client_id
    others = [
        _client_desktop_view(shell, desktop, client_id)
        for desktop in shell.list_desktops()
        if desktop.id != target.desktop.id
    ]
    choice = choose_show_target(
        _client_desktop_view(shell, target.desktop, client_id), others, app, path, showing, repoint
    )
    desktop = shell.get_desktop(choice.desktop_id)
    is_raised_in_own_window = False
    if choice.window is None:
        request = WindowOpenRequest(app=app, path=path, client_id=client_id, if_present=IfPresent.NEW)
        window_id = shell.open_window(desktop.id, request).window.id
    else:
        window_id = choice.window.id
        if choice.outcome is not ShowOutcome.RAISED:
            # As a ``navigate`` does: an independent window moves for this client alone, a linked one for everyone.
            shell.report_window_location(desktop.id, window_id, client_id, path, choice.window.title)
        is_raised_in_own_window = placement_of(shell.read_desktop_layout(desktop, client_id), window_id).is_detached
        if not is_raised_in_own_window:
            shell.edit_desktop_layout(desktop, client_id, lambda current: with_window_raised(current, window_id))
    # Switched after the raise, so the layout the client fetches on arriving already has the window on top; a window
    # raised in its own window leaves the client where ``args.desktop`` put it.
    if not is_raised_in_own_window and desktop.id != target.desktop.id:
        shell.set_client_active_desktop(client_id, desktop.id)
    else:
        _switch_as_asked(shell, target)
    _announce_window_op(shell, LayoutOp.SHOW, window_id, client_id, requester, is_detached=is_raised_in_own_window)
    logger.info(
        "layout op={} requester={} desktop={} client={} app={} path={} shown={}",
        LayoutOp.SHOW,
        requester,
        desktop.id,
        client_id,
        app,
        path,
        choice.outcome.value,
    )
    shown_on = _DesktopOpTarget(client_id=client_id, desktop=desktop)
    notes = _PopOutNotes(
        is_raised_in_own_window=is_raised_in_own_window,
        has_no_desktop_window=not is_raised_in_own_window and _has_no_desktop_window(shell, client_id),
    )
    answer = ShowAnswer.model_validate(
        {**dict(_answer_document(shell, shown_on, window_id, notes)), "window_id": window_id, "shown": choice.outcome}
    )
    return jsonify(answer.model_dump(mode="json"))


def _beside_anchor(
    shell: ShellState, arguments: OpenArgs, target: _DesktopOpTarget, requester: OpRequester | None
) -> Window | None:
    """The window an ``open``'s ``beside`` names, resolved before the window is opened; None when it names none.

    The pairing is the open's courtesy, not its point: an open whose ``beside`` names no window on this desktop --
    a chat the user closed, an op from nobody's chat -- still opens its window, where it would have landed. A
    spelling that is no window at all never gets here: the op's arguments refuse it.
    """
    if arguments.beside is None:
        return None
    try:
        return _named_window(shell, target, arguments.beside, requester).window
    except (WindowNotFoundError, NoRequesterWindowError):
        logger.info(
            "open beside={} matched no window on desktop {}; leaving the window as placed",
            arguments.beside,
            target.desktop.id,
        )
        return None


@pure
def _with_pair_placed(
    layout: DesktopLayout,
    anchor_id: WindowId,
    anchor_frame: Frame | None,
    is_anchor_hidden: bool,
    window_id: WindowId,
    opened: Frame,
) -> DesktopLayout:
    """The layout with a paired window at ``opened`` and on top, and its anchor at ``anchor_frame`` when the rule
    moved it (None when it did not)."""
    if anchor_frame is not None:
        placed = with_window_frame(layout, anchor_id, anchor_frame)
    elif is_anchor_hidden:
        # Where it stands is where it belongs, but it is not on screen to stand there.
        placed = with_window_raised(layout, anchor_id)
    else:
        # Not touched at all, so its state survives: a window already snapped stays snapped rather than
        # being rewritten as the same rectangle in normal.
        placed = layout
    return with_window_frame(placed, window_id, opened)


def _pair_beside(shell: ShellState, target: _DesktopOpTarget, anchor: Window | None, window_id: WindowId) -> None:
    """Lay the window an ``open`` landed on beside ``anchor``, for the target client alone, at the frames
    ``paired_frames`` gives: the opened one against the anchor and on top of the stack, and the anchor wherever the
    rule leaves it, which is usually exactly where it was. An ``open`` that answered the anchor itself has nothing
    to pair it with. An anchor out of sight (minimized, or pulled out, which only a forced open pairs with) is
    shown where it stands."""
    if anchor is None or anchor.id == window_id:
        return
    # The desktop as the open left it: an edit reads the layout against the windows its desktop holds, and the
    # snapshot the op started from is one window short.
    desktop = shell.get_desktop(target.desktop.id)
    placement = placement_of(shell.read_desktop_layout(desktop, target.client_id), anchor.id)
    standing = frame_for_state(placement.frame, placement.state)
    kept, opened = paired_frames(standing)
    is_anchor_hidden = placement.is_minimized or placement.is_detached
    shell.edit_desktop_layout(
        desktop,
        target.client_id,
        lambda current: _with_pair_placed(
            current, anchor.id, kept if kept != standing else None, is_anchor_hidden, window_id, opened
        ),
    )


def _open(
    shell: ShellState, arguments: OpenArgs, target: _DesktopOpTarget, requester: OpRequester | None
) -> tuple[WindowId, _PopOutNotes]:
    """The ``open`` op for a settled client: a window found at the path that the client has popped out is raised in
    its own window rather than pulled back; a ``beside`` naming a popped-out window opens the window unpaired unless
    the open is forced, which brings that window back to pair with."""
    anchor = _beside_anchor(shell, arguments, target, requester)
    layout = shell.read_desktop_layout(target.desktop, target.client_id)
    unpaired_beside: WindowId | None = None
    is_anchor_detached = anchor is not None and placement_of(layout, anchor.id).is_detached
    if (
        anchor is not None
        and _is_detached_while_connected(shell, target.client_id, is_anchor_detached)
        and not arguments.force
    ):
        unpaired_beside = anchor.id
        anchor = None
    request = _open_request(shell, arguments, target.client_id, target.desktop.id)
    found = (
        find_window_at(target.desktop, request.app, request.path)
        if request.if_present is IfPresent.FOCUS and not arguments.minimized
        else None
    )
    is_found_detached = found is not None and placement_of(layout, found.id).is_detached
    if found is not None and _is_detached_while_connected(shell, target.client_id, is_found_detached):
        _raise_in_own_window(shell, found.id, target.client_id, requester)
        return found.id, _PopOutNotes(is_raised_in_own_window=True, unpaired_beside=unpaired_beside)
    window_id = shell.open_window(target.desktop.id, request).window.id
    _pair_beside(shell, target, anchor, window_id)
    _switch_as_asked(shell, target)
    if not arguments.minimized:
        _announce_window_op(shell, LayoutOp.OPEN, window_id, target.client_id, requester)
    return window_id, _PopOutNotes(
        is_brought_back=is_found_detached or (anchor is not None and anchor.id != window_id and is_anchor_detached),
        unpaired_beside=unpaired_beside,
        has_no_desktop_window=not arguments.minimized and _has_no_desktop_window(shell, target.client_id),
    )


def _op_context(shell: ShellState, requester: OpRequester | None) -> ResponseReturnValue:
    clients = summarize_client_activity(shell.activity.read_events(), shell.broadcaster.get_connected_client_infos())
    logger.info("layout op=context requester={} clients={}", requester, len(clients))
    return jsonify(ContextAnswer(clients=tuple(clients)).model_dump(mode="json"))


def dispatch_desktop_op(shell: ShellState, body: OpBody) -> ResponseReturnValue:
    """Apply one verb of the op route (desktop contracts.md section 8): ``context`` answers the clients' recent
    activity, the inventory ops the inventory document, a whole-app ``refresh`` and the interface reload reach every
    client; the rest resolve their client and desktop, edit the files, and answer the resulting state."""
    requester = body.requester
    match body:
        case ContextBody():
            return _op_context(shell, requester)
        case InventoryBody():
            document = inventory_document(shell)
            logger.info("layout op={} requester={} desktops={}", body.op, requester, len(document.desktops))
            return jsonify(InventoryOpAnswer.model_validate(dict(document)).model_dump(mode="json"))
        case ReloadSystemInterfaceBody():
            return _reload_system_interface(shell, requester)
        case RefreshAppBody(args=arguments):
            return _refresh_app(shell, arguments.app, requester)
        case RefreshWindowBody(args=arguments):
            target = _resolve_target(shell, arguments, requester)
            refreshed = _refresh_window(shell, arguments, target, requester)
            _switch_as_asked(shell, target)
            return refreshed
        case OpenBody(args=arguments):
            # An open is the one client-scoped op that still means something with no client: the window is shared.
            if resolve_client(shell, arguments.client, requester) is None:
                return _open_unplaced(shell, arguments, requester)
            target = _resolve_target(shell, arguments, requester)
            window_id, notes = _open(shell, arguments, target, requester)
            return _applied(shell, body, target, window_id, notes)
        case LoadBody(args=arguments):
            target = _resolve_target(shell, arguments, requester)
            _switch_as_asked(shell, target)
            return _applied(shell, body, target, None, _PopOutNotes())
        case ShowBody(args=arguments):
            return _show(shell, arguments, _resolve_target(shell, arguments, requester), requester)
        case WindowOpBody(args=arguments):
            target = _resolve_target(shell, arguments, requester)
            window_id, notes = _op_window(shell, body.op, arguments, target, requester)
            notes = _after_window_op(shell, body.op, target, window_id, notes, requester)
            return _applied(shell, body, target, window_id, notes)
        case PlaceBody(args=arguments):
            target = _resolve_target(shell, arguments, requester)
            window_id, notes = _place(shell, arguments, target, requester)
            notes = _after_window_op(shell, body.op, target, window_id, notes, requester)
            return _applied(shell, body, target, window_id, notes)
        case NavigateBody(args=arguments):
            target = _resolve_target(shell, arguments, requester)
            window_id = _navigate(shell, arguments, target, requester)
            _switch_as_asked(shell, target)
            return _applied(shell, body, target, window_id, _PopOutNotes())
        case ShortcutsBody() | ShortcutSetBody() | ShortcutMoveBody() | ShortcutRemoveBody() | WallpaperBody():
            target = _resolve_target(shell, body.args, requester)
            _op_shortcuts(shell, body, target)
            _switch_as_asked(shell, target)
            return _applied(shell, body, target, None, _PopOutNotes())
        case _:
            assert_never(body)


def _announce_window_op(
    shell: ShellState,
    op: LayoutOp,
    window_id: WindowId,
    client_id: ClientId,
    requester: OpRequester | None,
    is_detached: bool | None = None,
) -> None:
    """Tell the target client's windows which window an op put in front of it, so a page that shows one window at a
    time (the phone layout) can switch to it, and, on a ``show`` naming a pulled-out window, the desktop's page and
    that window's solo page ask the embedder to raise its own desktop window; the op's own edit has already been
    written."""
    shell.broadcaster.broadcast_layout_op(
        LayoutOpMessage(
            op=op,
            args=LayoutOpMessageArgs(window=window_id, is_detached=is_detached),
            requester=requester_spelling(requester),
            target_client_id=client_id,
        )
    )


def _transient_answer(target_client_id: ClientId | None) -> ResponseReturnValue:
    return jsonify(TransientOpAnswer(target_client_id=target_client_id).model_dump(mode="json"))


def _reload_system_interface(shell: ShellState, requester: OpRequester | None) -> ResponseReturnValue:
    """The transient interface reload: every window of the shell, on every client."""
    shell.broadcaster.broadcast_layout_op(
        LayoutOpMessage(
            op=LayoutOp.RELOAD_SYSTEM_INTERFACE,
            args=LayoutOpMessageArgs(),
            requester=requester_spelling(requester),
            target_client_id=None,
        )
    )
    logger.info("layout op={} requester={} (every client)", LayoutOp.RELOAD_SYSTEM_INTERFACE, requester)
    return _transient_answer(None)


def _refresh_app(shell: ShellState, app: AppName, requester: OpRequester | None) -> ResponseReturnValue:
    """The transient whole-app ``refresh``: every page of the app on every client, so no client is targeted."""
    shell.require_app_entry(str(app))
    shell.broadcaster.broadcast_layout_op(
        LayoutOpMessage(
            op=LayoutOp.REFRESH,
            args=LayoutOpMessageArgs(app=app),
            requester=requester_spelling(requester),
            target_client_id=None,
        )
    )
    logger.info("layout op=refresh requester={} app={} (every client)", requester, app)
    return _transient_answer(None)


def _refresh_window(
    shell: ShellState, arguments: RefreshWindowArgs, target: _DesktopOpTarget, requester: OpRequester | None
) -> ResponseReturnValue:
    """The transient one-window ``refresh``: the window's page on the target client."""
    window = _named_window(shell, target, arguments.window, requester).window
    shell.broadcaster.broadcast_layout_op(
        LayoutOpMessage(
            op=LayoutOp.REFRESH,
            args=LayoutOpMessageArgs(window=window.id),
            requester=requester_spelling(requester),
            target_client_id=target.client_id,
        )
    )
    logger.info("layout op=refresh requester={} window={} client={}", requester, window.id, target.client_id)
    return _transient_answer(target.client_id)
