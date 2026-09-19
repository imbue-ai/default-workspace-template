"""The desktop interface's routes (desktop-interface contracts.md sections 5 and 8): desktops, windows, placements,
wallpapers, and the desktop verbs of the op route, served beside the tabbed shell's routes until it is deleted."""

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final
from urllib.parse import urlencode

from app_instances.blueprint import parse_request_body
from app_manifest.manifest import ShortcutMode
from app_manifest.manifest import describe_validation_error
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from flask import Flask
from flask import Response
from flask import jsonify
from flask import request
from flask import send_file
from flask.typing import ResponseReturnValue
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.app_context import get_state
from imbue.system_interface.shell.data_types import Desktop
from imbue.system_interface.shell.data_types import DesktopLayout
from imbue.system_interface.shell.data_types import DesktopShortcut
from imbue.system_interface.shell.data_types import Frame
from imbue.system_interface.shell.data_types import GridCell
from imbue.system_interface.shell.data_types import PlacementsSaveRequest
from imbue.system_interface.shell.data_types import ShortcutTarget
from imbue.system_interface.shell.data_types import Wallpaper
from imbue.system_interface.shell.data_types import Window
from imbue.system_interface.shell.data_types import WindowLocationReport
from imbue.system_interface.shell.data_types import WindowOpenRequest
from imbue.system_interface.shell.data_types import desktop_layout_wire_json
from imbue.system_interface.shell.data_types import desktop_wire_json
from imbue.system_interface.shell.data_types import effective_launch_paths
from imbue.system_interface.shell.desktop_document import default_launch_path_id
from imbue.system_interface.shell.desktop_document import effective_placements
from imbue.system_interface.shell.desktop_document import most_recently_focused_window_of_app
from imbue.system_interface.shell.desktop_document import next_shortcut_cell
from imbue.system_interface.shell.desktop_document import path_carries_marker
from imbue.system_interface.shell.desktop_document import require_window
from imbue.system_interface.shell.desktop_document import with_window_frame
from imbue.system_interface.shell.desktop_document import with_window_minimized
from imbue.system_interface.shell.desktop_document import with_window_raised
from imbue.system_interface.shell.desktop_document import with_window_restored
from imbue.system_interface.shell.desktop_document import with_window_state
from imbue.system_interface.shell.desktops import find_desktop_by_name_or_id
from imbue.system_interface.shell.errors import DesktopNotFoundError
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.errors import LayoutOpError
from imbue.system_interface.shell.errors import WallpaperNotFoundError
from imbue.system_interface.shell.errors import WindowNotFoundError
from imbue.system_interface.shell.layout_ops import DESKTOP_READ_OPS
from imbue.system_interface.shell.layout_ops import DESKTOP_SHORTCUT_OPS
from imbue.system_interface.shell.layout_ops import DesktopOpArguments
from imbue.system_interface.shell.layout_ops import LOAD_OP
from imbue.system_interface.shell.layout_ops import OpRequester
from imbue.system_interface.shell.layout_ops import SELF_ADDRESS
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import SharingMode
from imbue.system_interface.shell.primitives import WallpaperKind
from imbue.system_interface.shell.primitives import WallpaperName
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowState
from imbue.system_interface.shell.route_helpers import HTTP_CREATED
from imbue.system_interface.shell.route_helpers import HTTP_NO_CONTENT
from imbue.system_interface.shell.route_helpers import HTTP_OK
from imbue.system_interface.shell.route_helpers import op_only_args
from imbue.system_interface.shell.route_helpers import require_client
from imbue.system_interface.shell.state import ShellState
from imbue.system_interface.shell.wallpapers import BUNDLED_WALLPAPERS_DIRNAME
from imbue.system_interface.shell.wallpapers import WALLPAPER_ROUTE_PREFIX
from imbue.system_interface.shell.wallpapers import WallpaperDirectories
from imbue.system_interface.shell.wallpapers import list_wallpapers
from imbue.system_interface.shell.wallpapers import resolve_wallpaper_file
from imbue.system_interface.shell.wallpapers import wallpaper_listing_wire_json

# The ``place`` op's zones (desktop contracts.md section 8), by the state each sets.
_ZONE_STATES: Final[dict[str, WindowState]] = {
    "left": WindowState.SNAPPED_LEFT,
    "right": WindowState.SNAPPED_RIGHT,
    "maximized": WindowState.MAXIMIZED,
}
_FRAME_COMPONENT_COUNT: Final[int] = 4
_CELL_COMPONENT_COUNT: Final[int] = 2


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
    sharing: SharingMode = Field(description="shared or personal")


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
    return jsonify({"desktops": [desktop_wire_json(desktop) for desktop in _shell().list_desktops()]})


def create_desktop() -> ResponseReturnValue:
    body = parse_request_body(DesktopMetadataRequest)
    shell = _shell()
    desktop = shell.desktops.create_desktop(body.name, body.color, body.glyph, shell.seed_shortcuts())
    shell.broadcast_desktops_updated()
    return jsonify(desktop_wire_json(desktop)), HTTP_CREATED


def update_desktop_settings(desktop_id: str) -> ResponseReturnValue:
    body = parse_request_body(DesktopSettingsRequest)
    shell = _shell()
    desktop = shell.desktops.update_settings(desktop_id, body.name, body.color, body.glyph, body.sharing)
    shell.broadcast_desktops_updated()
    return jsonify(desktop_wire_json(desktop))


def set_desktop_wallpaper(desktop_id: str) -> ResponseReturnValue:
    body = parse_request_body(DesktopWallpaperRequest)
    shell = _shell()
    desktop = shell.desktops.set_wallpaper(desktop_id, _existing_wallpaper(body.wallpaper))
    shell.broadcast_desktops_updated()
    return jsonify(desktop_wire_json(desktop))


def delete_desktop(desktop_id: str) -> ResponseReturnValue:
    outcome = _shell().delete_desktop(desktop_id)
    return jsonify({"fallback_desktop_id": str(outcome.fallback_desktop_id)})


def set_desktop_shortcut(desktop_id: str) -> ResponseReturnValue:
    body = parse_request_body(DesktopShortcutRequest)
    shell = _shell()
    shortcut = DesktopShortcut(target=_validated_target(shell, body.target), mode=body.mode, cell=body.cell)
    desktop = shell.desktops.set_shortcut(desktop_id, shortcut)
    shell.broadcast_desktops_updated()
    return jsonify(desktop_wire_json(desktop))


def move_desktop_shortcut(desktop_id: str) -> ResponseReturnValue:
    body = parse_request_body(ShortcutMoveRequest)
    shell = _shell()
    desktop = shell.desktops.move_shortcut(desktop_id, body.app, body.launch, body.cell)
    shell.broadcast_desktops_updated()
    return jsonify(desktop_wire_json(desktop))


def remove_desktop_shortcut(desktop_id: str) -> ResponseReturnValue:
    body = parse_request_body(ShortcutRemoveRequest)
    shell = _shell()
    desktop = shell.desktops.remove_shortcut(desktop_id, body.app, body.launch)
    shell.broadcast_desktops_updated()
    return jsonify(desktop_wire_json(desktop))


# Section 5.3: windows


def open_window(desktop_id: str) -> ResponseReturnValue:
    body = parse_request_body(WindowOpenRequest)
    outcome = _shell().open_window(desktop_id, body)
    return (
        jsonify({"window": outcome.window.model_dump(mode="json"), "is_new": outcome.is_new}),
        HTTP_CREATED if outcome.is_new else HTTP_OK,
    )


def close_window(desktop_id: str, window_id: str) -> ResponseReturnValue:
    _shell().close_window(desktop_id, WindowId(window_id))
    return "", HTTP_NO_CONTENT


def report_window_location(desktop_id: str, window_id: str) -> ResponseReturnValue:
    body = parse_request_body(WindowLocationReport)
    window = _shell().report_window_location(desktop_id, WindowId(window_id), body.path, body.title)
    return jsonify(window.model_dump(mode="json"))


# Section 5.4: placements


def get_placements(desktop_id: str) -> ResponseReturnValue:
    client_id = ClientId(request.args.get("client", ""))
    return jsonify(desktop_layout_wire_json(_shell().read_desktop_layout(desktop_id, client_id)))


def save_placements(desktop_id: str) -> ResponseReturnValue:
    body = parse_request_body(PlacementsSaveRequest)
    saved = _shell().save_browser_placements(desktop_id, body)
    # The stamp is spelled as the read route spells it, so the window compares like with like.
    return jsonify({"updated_at": desktop_layout_wire_json(saved)["updated_at"] if saved is not None else None})


# Section 5.5: wallpapers, and the inventory's desktop fields


def list_wallpapers_route() -> ResponseReturnValue:
    return jsonify(
        {"wallpapers": [wallpaper_listing_wire_json(listing) for listing in list_wallpapers(_wallpaper_directories())]}
    )


def serve_wallpaper(kind: str, name: str) -> Response:
    try:
        wallpaper = Wallpaper(kind=WallpaperKind(kind), name=WallpaperName(name))
    except (ValueError, InvalidShellValueError):
        return Response(status=404)
    path = resolve_wallpaper_file(wallpaper, _wallpaper_directories())
    if path is None:
        return Response(status=404)
    return send_file(path)


@pure
def _shown_window_ids(desktop: Desktop, layout: DesktopLayout) -> list[str]:
    return [
        str(placement.window_id) for placement in effective_placements(layout, desktop) if not placement.is_minimized
    ]


def desktop_inventory_fields(shell: ShellState, clients: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The inventory document's desktop fields (desktop contracts.md section 5.5): every desktop, and each client
    with ``shown``, the windows of its active desktop that its layout does not minimize."""
    desktops = shell.list_desktops()
    desktops_by_id = {desktop.id: desktop for desktop in desktops}
    clients_with_shown: list[dict[str, Any]] = []
    for client in clients:
        active = shell.active_desktop_of_client(str(client["id"]))
        desktop = desktops_by_id.get(active) if active is not None else None
        shown = (
            _shown_window_ids(
                desktop,
                shell.placements.read_layout(desktop.id, str(client["id"]), {window.id for window in desktop.windows}),
            )
            if desktop is not None
            else []
        )
        clients_with_shown.append(
            {**client, "active_desktop": str(active) if active is not None else None, "shown": shown}
        )
    return {"desktops": [desktop_wire_json(desktop) for desktop in desktops], "clients": clients_with_shown}


def register_desktop_routes(application: Flask) -> None:
    """Register the desktop interface's routes on ``application`` (the error handlers are the shell's)."""
    application.add_url_rule("/api/desktops", view_func=list_desktops, methods=["GET"], endpoint="list_desktops")
    application.add_url_rule("/api/desktops", view_func=create_desktop, methods=["POST"], endpoint="create_desktop")
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


def _parse_desktop_arguments(args_raw: Mapping[str, Any]) -> DesktopOpArguments:
    try:
        return DesktopOpArguments.model_validate(op_only_args(args_raw))
    except ValidationError as e:
        raise LayoutOpError(f"bad op arguments: {describe_validation_error(e)}") from e


def _requested_desktop(args_raw: Mapping[str, Any]) -> str | None:
    requested = args_raw.get("desktop")
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
def _app_name_or_raise(raw: str, what: str) -> AppName:
    """An op argument that must be an app name; a ``<kind>:``-style spelling or an address is refused with the fix."""
    if ":" in raw:
        raise LayoutOpError(f"{what} {raw!r} is not an app name: give an app name and a path")
    try:
        return AppName(raw)
    except ValueError as e:
        raise LayoutOpError(f"{what} {raw!r} is not an app name: {e}") from e


def _resolve_window(desktop: Desktop, layout: DesktopLayout, raw: str, requester: OpRequester | None) -> Window:
    """The window an op names: a window id, ``self`` (the requester's app's window whose path carries its marker),
    or an app name (that app's most recently focused window in this client's layout)."""
    if not raw:
        raise LayoutOpError("this op needs a window: a window id, 'self', or an app name")
    if raw == SELF_ADDRESS:
        if requester is None or not requester.marker:
            raise LayoutOpError(
                "'self' names the requester's own window, but this op carried no requester with a marker"
            )
        own = next(
            (
                window
                for window in desktop.windows
                if window.app == requester.app and path_carries_marker(window.path, requester.marker)
            ),
            None,
        )
        if own is None:
            raise WindowNotFoundError(f"self ({requester.app} carrying {requester.marker!r} on desktop {desktop.id})")
        return own
    try:
        window_id = WindowId(raw)
    except InvalidShellValueError:
        window_id = None
    if window_id is not None:
        return require_window(desktop, window_id)
    app = _app_name_or_raise(raw, "window")
    window = most_recently_focused_window_of_app(layout, desktop, app)
    if window is None:
        raise WindowNotFoundError(f"{app} (no window of it on desktop {desktop.id})")
    return window


@pure
def _parse_frame(raw: str) -> Frame:
    parts = raw.split(",")
    if len(parts) != _FRAME_COMPONENT_COUNT:
        raise LayoutOpError(f"a frame is 'x,y,width,height' in fractions, not {raw!r}")
    try:
        x, y, width, height = (float(part) for part in parts)
        return Frame(x=x, y=y, width=width, height=height)
    except (ValueError, ValidationError) as e:
        raise LayoutOpError(f"a frame is 'x,y,width,height' in fractions inside the unit square: {e}") from e


@pure
def _parse_cell(raw: str) -> GridCell:
    parts = raw.split(",")
    if len(parts) != _CELL_COMPONENT_COUNT:
        raise LayoutOpError(f"a cell is 'column,row', not {raw!r}")
    try:
        column, row = (int(part) for part in parts)
        return GridCell(column=column, row=row)
    except (ValueError, ValidationError) as e:
        raise LayoutOpError(f"a cell is 'column,row' with both at least zero: {e}") from e


@pure
def _launch_query(params: Mapping[str, str]) -> str:
    return f"?{urlencode(dict(params))}" if params else ""


def _open_request(shell: ShellState, arguments: DesktopOpArguments, client_id: ClientId) -> WindowOpenRequest:
    """What an ``open`` op opens: an explicit path, else the launch path it names, the app's default, or its first,
    with the params as the query string."""
    app = _app_name_or_raise(arguments.app, "app")
    entry = shell.require_app_entry(str(app))
    if arguments.path:
        if arguments.launch is not None or arguments.params:
            raise LayoutOpError("an open names a path or a launch path, not both")
        return WindowOpenRequest(
            app=app, path=WindowPath(arguments.path), client_id=client_id, if_present=arguments.if_present, launch=None
        )
    offered = effective_launch_paths(entry.row)
    default_launch = default_launch_path_id(entry.row)
    launch_id = (
        arguments.launch
        if arguments.launch is not None
        else (default_launch if default_launch is not None else offered[0].id)
    )
    launch = next((candidate for candidate in offered if candidate.id == launch_id), None)
    if launch is None:
        raise LayoutOpError(f"App {str(app)!r} declares no launch path {str(launch_id)!r}")
    return WindowOpenRequest(
        app=app,
        path=WindowPath(f"{launch.path}{_launch_query(arguments.params)}"),
        client_id=client_id,
        if_present=arguments.if_present,
        launch=launch.id,
    )


def _answer(shell: ShellState, target: _DesktopOpTarget, window_id: WindowId | None) -> ResponseReturnValue:
    desktop = shell.get_desktop(target.desktop.id)
    layout = shell.read_desktop_layout(desktop.id, target.client_id)
    return jsonify(
        {
            "ok": True,
            "desktop_id": str(desktop.id),
            "client_id": str(target.client_id),
            "desktop": desktop_wire_json(desktop),
            "layout": desktop_layout_wire_json(layout),
            "window_id": str(window_id) if window_id is not None else None,
        }
    )


@pure
def _required_launch(arguments: DesktopOpArguments, op: str) -> LaunchPathId:
    if arguments.launch is None:
        raise LayoutOpError(f"{op} needs a launch path id in args.launch")
    return arguments.launch


def _op_shortcuts(shell: ShellState, op: str, arguments: DesktopOpArguments, target: _DesktopOpTarget) -> None:
    desktop_id = target.desktop.id
    match op:
        case "shortcuts":
            return
        case "shortcut_set":
            shortcut_target = _validated_target(
                shell,
                ShortcutTarget(app=_app_name_or_raise(arguments.app, "app"), launch=_required_launch(arguments, op)),
            )
            cell = _parse_cell(arguments.cell) if arguments.cell else next_shortcut_cell(target.desktop)
            shell.desktops.set_shortcut(
                desktop_id, DesktopShortcut(target=shortcut_target, mode=arguments.mode, cell=cell)
            )
        case "shortcut_move":
            if not arguments.cell:
                raise LayoutOpError("shortcut_move needs a cell ('column,row')")
            shell.desktops.move_shortcut(
                desktop_id,
                _app_name_or_raise(arguments.app, "app"),
                _required_launch(arguments, op),
                _parse_cell(arguments.cell),
            )
        case "shortcut_remove":
            shell.desktops.remove_shortcut(
                desktop_id, _app_name_or_raise(arguments.app, "app"), _required_launch(arguments, op)
            )
        case "wallpaper":
            shell.desktops.set_wallpaper(desktop_id, _existing_wallpaper(arguments.wallpaper))
        case _:
            raise LayoutOpError(f"Op {op!r} has no shortcut handler")
    shell.broadcast_desktops_updated()


def _op_window(
    shell: ShellState, op: str, arguments: DesktopOpArguments, target: _DesktopOpTarget, requester: OpRequester | None
) -> WindowId:
    desktop = target.desktop
    layout = shell.read_desktop_layout(desktop.id, target.client_id)
    window = _resolve_window(desktop, layout, arguments.window, requester)
    match op:
        case "focus":
            shell.edit_desktop_layout(
                desktop, target.client_id, lambda current: with_window_raised(current, window.id)
            )
        case "minimize":
            shell.edit_desktop_layout(
                desktop, target.client_id, lambda current: with_window_minimized(current, window.id)
            )
        case "restore":
            shell.edit_desktop_layout(
                desktop, target.client_id, lambda current: with_window_restored(current, window.id)
            )
        case "maximize":
            shell.edit_desktop_layout(
                desktop, target.client_id, lambda current: with_window_state(current, window.id, WindowState.MAXIMIZED)
            )
        case "place":
            if arguments.zone and arguments.frame:
                raise LayoutOpError("place takes a zone or a frame, not both")
            if arguments.zone:
                state = _ZONE_STATES.get(arguments.zone)
                if state is None:
                    raise LayoutOpError(f"a zone is one of {sorted(_ZONE_STATES)}, not {arguments.zone!r}")
                shell.edit_desktop_layout(
                    desktop, target.client_id, lambda current: with_window_state(current, window.id, state)
                )
            elif arguments.frame:
                frame = _parse_frame(arguments.frame)
                shell.edit_desktop_layout(
                    desktop, target.client_id, lambda current: with_window_frame(current, window.id, frame)
                )
            else:
                raise LayoutOpError("place needs a zone (left, right, maximized) or a frame (x,y,width,height)")
        case "close":
            shell.close_window(desktop.id, window.id)
        case "navigate":
            if not arguments.path:
                raise LayoutOpError("navigate needs a path")
            shell.report_window_location(desktop.id, window.id, WindowPath(arguments.path), window.title)
        case _:
            raise LayoutOpError(f"Op {op!r} has no window handler")
    return window.id


def dispatch_desktop_op(
    shell: ShellState, op: str, args_raw: Mapping[str, Any], requester: OpRequester | None
) -> ResponseReturnValue:
    """Apply one desktop verb (desktop contracts.md section 8): the read-only ones answer the desktops or the
    inventory; the rest resolve their client and desktop, edit the files, and answer the resulting state."""
    if op in DESKTOP_READ_OPS:
        desktops = [desktop_wire_json(desktop) for desktop in shell.list_desktops()]
        logger.info("layout op={} requester={} desktops={}", op, requester, len(desktops))
        return jsonify({"ok": True, "desktops": desktops})
    arguments = _parse_desktop_arguments(args_raw)
    target = _resolve_target(shell, args_raw, requester)
    window_id: WindowId | None = None
    if op == LOAD_OP:
        if _requested_desktop(args_raw) is None:
            raise LayoutOpError("'load' requires a desktop name in args.desktop")
    elif op == "open":
        window_id = shell.open_window(target.desktop.id, _open_request(shell, arguments, target.client_id)).window.id
    elif op == "refresh":
        return _op_refresh(shell, arguments, target, requester)
    elif op in DESKTOP_SHORTCUT_OPS:
        _op_shortcuts(shell, op, arguments, target)
    else:
        window_id = _op_window(shell, op, arguments, target, requester)
    logger.info(
        "layout op={} requester={} desktop={} client={} args={}",
        op,
        requester,
        target.desktop.id,
        target.client_id,
        args_raw,
    )
    return _answer(shell, target, window_id)


def _op_refresh(
    shell: ShellState, arguments: DesktopOpArguments, target: _DesktopOpTarget, requester: OpRequester | None
) -> ResponseReturnValue:
    """The transient ``refresh``: one window's page on the target client, or every page of an app on every client."""
    requester_wire = (
        "" if requester is None else str(requester.app) + (f":{requester.marker}" if requester.marker else "")
    )
    if arguments.app:
        app = _app_name_or_raise(arguments.app, "app")
        shell.require_app_entry(str(app))
        shell.broadcaster.broadcast_layout_op(
            "refresh", {"app": str(app)}, requester=requester_wire, target_client_id=None
        )
        logger.info("layout op=refresh requester={} app={} (every client)", requester, app)
        return jsonify({"ok": True, "target_client_id": None})
    layout = shell.read_desktop_layout(target.desktop.id, target.client_id)
    window = _resolve_window(target.desktop, layout, arguments.window, requester)
    shell.broadcaster.broadcast_layout_op(
        "refresh", {"window": str(window.id)}, requester=requester_wire, target_client_id=str(target.client_id)
    )
    logger.info("layout op=refresh requester={} window={} client={}", requester, window.id, target.client_id)
    return jsonify({"ok": True, "target_client_id": str(target.client_id)})
