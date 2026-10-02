"""The desktop's routes (desktop-interface contracts.md sections 5 and 8): desktops, windows, placements, wallpapers,
and the verbs of the op route."""

from collections.abc import Mapping
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
from workspace_layout.answers import ShowAnswer
from workspace_layout.answers import TransientOpAnswer
from workspace_layout.ops import DESKTOP_ARG_KEY
from workspace_layout.ops import DesktopOpArguments
from workspace_layout.ops import OpRequester
from workspace_layout.ops import PLACEABLE_STATES
from workspace_layout.ops import parse_window_reference
from workspace_layout.ops import read_op_arguments
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
from imbue.system_interface.shell.desktop_document import frame_for_state
from imbue.system_interface.shell.desktop_document import most_recently_focused_window_of_app
from imbue.system_interface.shell.desktop_document import next_shortcut_cell
from imbue.system_interface.shell.desktop_document import paired_frames
from imbue.system_interface.shell.desktop_document import path_carries_marker
from imbue.system_interface.shell.desktop_document import placement_of
from imbue.system_interface.shell.desktop_document import require_window
from imbue.system_interface.shell.desktop_document import with_window_frame
from imbue.system_interface.shell.desktop_document import with_window_minimized
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


def inventory_document(shell: ShellState) -> InventoryDocument:
    """The one document of desktop contracts.md section 5.5: whether a preview shell answered, the workspace's name,
    every desktop, every app, and every known client with ``shown``, the windows of its active desktop that its layout
    does not minimize."""
    desktops = shell.list_desktops()
    desktops_by_id = {desktop.id: desktop for desktop in desktops}
    connected = shell.broadcaster.connected_client_ids()
    clients: list[InventoryClient] = []
    for record in shell.clients.list_clients():
        active = resolve_active_desktop(record, desktops)
        shown: tuple[WindowId, ...] = ()
        if active is not None:
            desktop = desktops_by_id[active]
            layout = shell.placements.read_layout(active, str(record.id), {window.id for window in desktop.windows})
            shown = _shown_window_ids(desktop, layout)
        view = client_view(_with_active_desktop(record, active), str(record.id) in connected)
        clients.append(InventoryClient.model_validate({**dict(view), "shown": shown}))
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


def _requested_desktop(args_raw: Mapping[str, Any]) -> str | None:
    requested = args_raw.get(DESKTOP_ARG_KEY)
    return requested if isinstance(requested, str) and requested else None


def _resolve_target(shell: ShellState, args_raw: Mapping[str, Any], requester: OpRequester | None) -> _DesktopOpTarget:
    """The client and desktop an op targets: ``args.desktop`` (by name or id, switching the client to it), else the
    client's active desktop by the rule of desktop contracts.md section 4.3."""
    client_id = require_client(shell, args_raw, requester)
    desktops = shell.list_desktops()
    requested = _requested_desktop(args_raw)
    if requested is not None:
        desktop = find_desktop_by_name_or_id(desktops, requested)
        if desktop is None:
            raise DesktopNotFoundError(requested)
        if shell.active_desktop_of_client(client_id) != desktop.id:
            shell.set_client_active_desktop(client_id, desktop.id)
        return _DesktopOpTarget(client_id=client_id, desktop=desktop)
    active = shell.active_desktop_of_client(client_id)
    if active is None:
        raise LayoutOpError("there is no desktop to edit yet")
    return _DesktopOpTarget(client_id=client_id, desktop=shell.get_desktop(active))


@pure
def _required_app(arguments: DesktopOpArguments, op: LayoutOp) -> AppName:
    if arguments.app is None:
        raise LayoutOpError(f"{op} needs an app in args.app")
    return arguments.app


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
    shell: ShellState, arguments: DesktopOpArguments, client_id: ClientId | None, desktop_id: DesktopId | None
) -> _OpenTarget:
    """What an ``open`` op opens: an explicit path, else the page of the launch path it names (the app's default, or
    its first), resolved as the launch route resolves one (a GET launch path with its params as the query, a POST one
    asked for its page with the client and its desktop as the envelope, or no envelope for an open with no client)."""
    app = _required_app(arguments, LayoutOp.OPEN)
    entry = shell.require_app_entry(str(app))
    if arguments.path is not None:
        if arguments.launch is not None or arguments.params:
            raise LayoutOpError("an open names a path or a launch path, not both")
        return _OpenTarget(app=app, path=arguments.path)
    default_launch = default_launch_path_id(entry.row)
    offered = effective_launch_paths(entry.row)
    launch_id = (
        arguments.launch
        if arguments.launch is not None
        else (default_launch if default_launch is not None else offered[0].id)
    )
    launch_path = shell.require_launch_path(entry, launch_id)
    return _OpenTarget(
        app=app, path=shell.launch_destination(entry, launch_path, arguments.params, client_id, desktop_id, None)
    )


def _open_request(
    shell: ShellState, arguments: DesktopOpArguments, client_id: ClientId, desktop_id: DesktopId
) -> WindowOpenRequest:
    target = _open_target(shell, arguments, client_id, desktop_id)
    return WindowOpenRequest(
        app=target.app,
        path=target.path,
        client_id=client_id,
        if_present=arguments.if_present,
        minimized=arguments.minimized,
    )


def _open_unplaced(
    shell: ShellState, arguments: DesktopOpArguments, args_raw: Mapping[str, Any], requester: OpRequester | None
) -> ResponseReturnValue:
    """An ``open`` with no client to target (desktop contracts.md section 8): the window is written on the named
    desktop, else the first, with no placement, so it shows minimized for every client rather than being refused."""
    desktops = shell.list_desktops()
    requested = _requested_desktop(args_raw)
    if requested is not None:
        desktop = find_desktop_by_name_or_id(desktops, requested)
        if desktop is None:
            raise DesktopNotFoundError(requested)
    elif desktops:
        desktop = desktops[0]
    else:
        raise LayoutOpError("there is no desktop to open on yet")
    target = _open_target(shell, arguments, None, None)
    outcome = shell.open_window_unplaced(desktop.id, target.app, target.path, arguments.if_present)
    logger.info("layout op=open requester={} desktop={} client=none args={}", requester, desktop.id, args_raw)
    answer = OpenAnswer(
        desktop_id=desktop.id,
        client_id=None,
        desktop=shell.desktop_view(shell.get_desktop(desktop.id)),
        layout=None,
        window_id=outcome.window.id,
    )
    return jsonify(answer.model_dump(mode="json"))


def _answer_document(shell: ShellState, target: _DesktopOpTarget, window_id: WindowId | None) -> DesktopOpAnswer:
    desktop = shell.get_desktop(target.desktop.id)
    return DesktopOpAnswer(
        desktop_id=desktop.id,
        client_id=target.client_id,
        desktop=shell.desktop_view(desktop),
        layout=_layout_view(shell, desktop, target.client_id),
        window_id=window_id,
    )


def _answer(shell: ShellState, target: _DesktopOpTarget, window_id: WindowId | None) -> ResponseReturnValue:
    return jsonify(_answer_document(shell, target, window_id).model_dump(mode="json"))


_ShortcutOp = Literal[
    LayoutOp.SHORTCUTS, LayoutOp.SHORTCUT_SET, LayoutOp.SHORTCUT_MOVE, LayoutOp.SHORTCUT_REMOVE, LayoutOp.WALLPAPER
]
_WindowOp = Literal[
    LayoutOp.FOCUS,
    LayoutOp.MINIMIZE,
    LayoutOp.RESTORE,
    LayoutOp.MAXIMIZE,
    LayoutOp.PLACE,
    LayoutOp.CLOSE,
    LayoutOp.NAVIGATE,
]


@pure
def _required_launch(arguments: DesktopOpArguments, op: LayoutOp) -> LaunchPathId:
    if arguments.launch is None:
        raise LayoutOpError(f"{op} needs a launch path id in args.launch")
    return arguments.launch


def _op_shortcuts(shell: ShellState, op: _ShortcutOp, arguments: DesktopOpArguments, target: _DesktopOpTarget) -> None:
    desktop_id = target.desktop.id
    match op:
        case LayoutOp.SHORTCUTS:
            return
        case LayoutOp.SHORTCUT_SET:
            shortcut_target = _validated_target(
                shell,
                ShortcutTarget(app=_required_app(arguments, op), launch=_required_launch(arguments, op)),
            )
            cell = arguments.cell if arguments.cell is not None else next_shortcut_cell(target.desktop)
            shell.desktops.set_shortcut(
                desktop_id, DesktopShortcut(target=shortcut_target, mode=arguments.mode, cell=cell)
            )
        case LayoutOp.SHORTCUT_MOVE:
            if arguments.cell is None:
                raise LayoutOpError("shortcut_move needs a cell")
            shell.desktops.move_shortcut(
                desktop_id, _required_app(arguments, op), _required_launch(arguments, op), arguments.cell
            )
        case LayoutOp.SHORTCUT_REMOVE:
            shell.desktops.remove_shortcut(desktop_id, _required_app(arguments, op), _required_launch(arguments, op))
        case LayoutOp.WALLPAPER:
            shell.desktops.set_wallpaper(desktop_id, _existing_wallpaper(arguments.wallpaper))
        case _:
            assert_never(op)
    shell.broadcast_desktops_updated()


def _op_window(
    shell: ShellState,
    op: _WindowOp,
    arguments: DesktopOpArguments,
    target: _DesktopOpTarget,
    requester: OpRequester | None,
) -> WindowId:
    desktop = target.desktop
    layout = shell.read_desktop_layout(desktop, target.client_id)
    seen_windows = shell.windows_for_client(desktop, target.client_id)
    window = _resolve_window(desktop, seen_windows, layout, arguments.window, requester)
    match op:
        case LayoutOp.FOCUS:
            shell.edit_desktop_layout(
                desktop, target.client_id, lambda current: with_window_raised(current, window.id)
            )
            _announce_window_op(shell, LayoutOp.FOCUS, window.id, target.client_id, requester)
        case LayoutOp.MINIMIZE:
            shell.edit_desktop_layout(
                desktop, target.client_id, lambda current: with_window_minimized(current, window.id)
            )
        case LayoutOp.RESTORE:
            shell.edit_desktop_layout(
                desktop, target.client_id, lambda current: with_window_restored(current, window.id)
            )
        case LayoutOp.MAXIMIZE:
            shell.edit_desktop_layout(
                desktop, target.client_id, lambda current: with_window_state(current, window.id, WindowState.MAXIMIZED)
            )
        case LayoutOp.PLACE:
            state = arguments.state
            frame = arguments.frame
            if state is not None and frame is not None:
                raise LayoutOpError("place takes a state or a frame, not both")
            if state is not None:
                shell.edit_desktop_layout(
                    desktop, target.client_id, lambda current: with_window_state(current, window.id, state)
                )
            elif frame is not None:
                shell.edit_desktop_layout(
                    desktop, target.client_id, lambda current: with_window_frame(current, window.id, frame)
                )
            else:
                raise LayoutOpError(
                    f"place needs a state (one of {[placeable.value for placeable in PLACEABLE_STATES]}) or a frame"
                )
        case LayoutOp.CLOSE:
            if window.is_pinned:
                raise LayoutOpError(f"window {window.id} is pinned and cannot be closed; minimize it instead")
            shell.close_window(desktop.id, window.id)
        case LayoutOp.NAVIGATE:
            if arguments.path is None:
                raise LayoutOpError("navigate needs a path")
            # As if the target client's page had reported it: an independent window moves for that client alone.
            seen = next(candidate for candidate in seen_windows if candidate.id == window.id)
            shell.report_window_location(desktop.id, window.id, target.client_id, arguments.path, seen.title)
        case _:
            assert_never(op)
    return window.id


def _client_desktop_view(shell: ShellState, desktop: Desktop, client_id: ClientId) -> ClientDesktopView:
    return ClientDesktopView(
        desktop=desktop,
        layout=shell.read_desktop_layout(desktop, client_id),
        seen_windows=shell.windows_for_client(desktop, client_id),
    )


def _show(
    shell: ShellState, arguments: DesktopOpArguments, target: _DesktopOpTarget, requester: OpRequester | None
) -> ResponseReturnValue:
    """The ``show`` op: put the path of the app on the target client's screen, choosing the window by the rule
    ``choose_show_target`` spells, and answer which way it went."""
    app = _required_app(arguments, LayoutOp.SHOW)
    shell.require_app_entry(str(app))
    if arguments.path is None:
        raise LayoutOpError("show needs a path")
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
    is_detached = False
    if choice.window is None:
        request = WindowOpenRequest(app=app, path=path, client_id=client_id, if_present=IfPresent.NEW)
        window_id = shell.open_window(desktop.id, request).window.id
    else:
        window_id = choice.window.id
        if choice.outcome is not ShowOutcome.RAISED:
            # As a ``navigate`` does: an independent window moves for this client alone, a linked one for everyone.
            shell.report_window_location(desktop.id, window_id, client_id, path, choice.window.title)
        # A pulled-out window's desktop window is the chrome's, which only the client's page can bring forward (on
        # the show message below); raising the placement would pull the window back onto the desktop instead.
        is_detached = placement_of(shell.read_desktop_layout(desktop, client_id), window_id).is_detached
        if not is_detached:
            shell.edit_desktop_layout(desktop, client_id, lambda current: with_window_raised(current, window_id))
            # Switched after the raise, so the layout the client fetches on arriving already has the window on top.
            if desktop.id != target.desktop.id:
                shell.set_client_active_desktop(client_id, desktop.id)
    _announce_window_op(shell, LayoutOp.SHOW, window_id, client_id, requester, is_detached=is_detached)
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
    answer = ShowAnswer.model_validate(
        {**dict(_answer_document(shell, shown_on, window_id)), "window_id": window_id, "shown": choice.outcome}
    )
    return jsonify(answer.model_dump(mode="json"))


def _beside_anchor(
    shell: ShellState, arguments: DesktopOpArguments, target: _DesktopOpTarget, requester: OpRequester | None
) -> Window | None:
    """The window an ``open``'s ``beside`` names, resolved before the window is opened; None when it names none.

    The pairing is the open's courtesy, not its point: an open whose ``beside`` names no window on this desktop --
    a chat the user closed, an op from nobody's chat -- still opens its window, where it would have landed. A
    spelling that is no window at all is the caller's mistake, and refuses the op here, while there is still nothing
    to leave behind.
    """
    if not arguments.beside:
        return None
    desktop = target.desktop
    try:
        return _resolve_window(
            desktop,
            shell.windows_for_client(desktop, target.client_id),
            shell.read_desktop_layout(desktop, target.client_id),
            arguments.beside,
            requester,
        )
    except (WindowNotFoundError, NoRequesterWindowError):
        logger.info(
            "open beside={} matched no window on desktop {}; leaving the window as placed",
            arguments.beside,
            desktop.id,
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
    to pair it with."""
    if anchor is None or anchor.id == window_id:
        return
    # The desktop as the open left it: an edit reads the layout against the windows its desktop holds, and the
    # snapshot the op started from is one window short.
    desktop = shell.get_desktop(target.desktop.id)
    placement = placement_of(shell.read_desktop_layout(desktop, target.client_id), anchor.id)
    standing = frame_for_state(placement.frame, placement.state)
    kept, opened = paired_frames(standing)
    shell.edit_desktop_layout(
        desktop,
        target.client_id,
        lambda current: _with_pair_placed(
            current, anchor.id, kept if kept != standing else None, placement.is_minimized, window_id, opened
        ),
    )


def _op_context(shell: ShellState, requester: OpRequester | None) -> ResponseReturnValue:
    clients = summarize_client_activity(shell.activity.read_events(), shell.broadcaster.get_connected_client_infos())
    logger.info("layout op=context requester={} clients={}", requester, len(clients))
    return jsonify(ContextAnswer(clients=tuple(clients)).model_dump(mode="json"))


def dispatch_desktop_op(
    shell: ShellState, op: LayoutOp, args_raw: Mapping[str, Any], requester: OpRequester | None
) -> ResponseReturnValue:
    """Apply one verb of the op route (desktop contracts.md section 8): ``context`` answers the clients' recent
    activity, the inventory ops the inventory document, a whole-app ``refresh`` and the interface reload reach every
    client; the rest resolve their client and desktop, edit the files, and answer the resulting state."""
    if op is LayoutOp.CONTEXT:
        return _op_context(shell, requester)
    if op is LayoutOp.DESKTOPS or op is LayoutOp.LIST:
        document = inventory_document(shell)
        logger.info("layout op={} requester={} desktops={}", op, requester, len(document.desktops))
        return jsonify(InventoryOpAnswer.model_validate(dict(document)).model_dump(mode="json"))
    arguments = read_op_arguments(args_raw)
    # The rules an op's own arguments settle come before any client is looked for, so a caller is told what to
    # fix rather than which client to name.
    if op is LayoutOp.LOAD and _requested_desktop(args_raw) is None:
        raise LayoutOpError("'load' requires a desktop name in args.desktop")
    if op is LayoutOp.OPEN and arguments.minimized and arguments.beside:
        raise LayoutOpError("'open' puts the window out of sight with minimized or beside another window, not both")
    if op is LayoutOp.REFRESH and arguments.app is not None:
        return _refresh_app(shell, arguments.app, requester)
    if op is LayoutOp.RELOAD_SYSTEM_INTERFACE:
        return _reload_system_interface(shell, requester)
    # An open is the one client-scoped op that still means something with no client: the window is shared.
    if op is LayoutOp.OPEN and resolve_client(shell, args_raw, requester) is None:
        return _open_unplaced(shell, arguments, args_raw, requester)
    target = _resolve_target(shell, args_raw, requester)
    window_id: WindowId | None = None
    match op:
        case LayoutOp.LOAD:
            # Resolving the target already switched the client to ``args.desktop``.
            pass
        case LayoutOp.OPEN:
            anchor = _beside_anchor(shell, arguments, target, requester)
            window_id = shell.open_window(
                target.desktop.id, _open_request(shell, arguments, target.client_id, target.desktop.id)
            ).window.id
            _pair_beside(shell, target, anchor, window_id)
            if not arguments.minimized:
                _announce_window_op(shell, LayoutOp.OPEN, window_id, target.client_id, requester)
        case LayoutOp.REFRESH:
            return _refresh_window(shell, arguments, target, requester)
        case LayoutOp.SHOW:
            return _show(shell, arguments, target, requester)
        case (
            LayoutOp.SHORTCUTS
            | LayoutOp.SHORTCUT_SET
            | LayoutOp.SHORTCUT_MOVE
            | LayoutOp.SHORTCUT_REMOVE
            | LayoutOp.WALLPAPER
        ):
            _op_shortcuts(shell, op, arguments, target)
        case (
            LayoutOp.FOCUS
            | LayoutOp.MINIMIZE
            | LayoutOp.RESTORE
            | LayoutOp.MAXIMIZE
            | LayoutOp.PLACE
            | LayoutOp.CLOSE
            | LayoutOp.NAVIGATE
        ):
            window_id = _op_window(shell, op, arguments, target, requester)
        case _:
            assert_never(op)
    logger.info(
        "layout op={} requester={} desktop={} client={} args={}",
        op,
        requester,
        target.desktop.id,
        target.client_id,
        args_raw,
    )
    return _answer(shell, target, window_id)


def _announce_window_op(
    shell: ShellState,
    op: LayoutOp,
    window_id: WindowId,
    client_id: ClientId,
    requester: OpRequester | None,
    is_detached: bool | None = None,
) -> None:
    """Tell the target client's windows which window an op put in front of it, so a page that shows one window at a
    time (the phone layout) can switch to it; the op's own edit has already been written."""
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
    shell: ShellState, arguments: DesktopOpArguments, target: _DesktopOpTarget, requester: OpRequester | None
) -> ResponseReturnValue:
    """The transient one-window ``refresh``: the window's page on the target client."""
    layout = shell.read_desktop_layout(target.desktop, target.client_id)
    seen_windows = shell.windows_for_client(target.desktop, target.client_id)
    window = _resolve_window(target.desktop, seen_windows, layout, arguments.window, requester)
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
