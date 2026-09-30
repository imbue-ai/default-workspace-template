from collections.abc import Mapping
from typing import Any
from typing import Final

from app_manifest.manifest import ShortcutMode
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from pydantic import Field

from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.primitives import ClientActivityKind
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import IfPresent
from workspace_layout.primitives import WallpaperKind
from workspace_layout.primitives import WallpaperName

# The ops the op route (desktop contracts.md section 8) dispatches on. Anything else is a 400.
CONTEXT_OP: Final[str] = "context"
LOAD_OP: Final[str] = "load"
# Put a path of an app on the target client's screen, choosing the window itself.
SHOW_OP: Final[str] = "show"
OPEN_OP: Final[str] = "open"
FOCUS_OP: Final[str] = "focus"
CLOSE_OP: Final[str] = "close"
NAVIGATE_OP: Final[str] = "navigate"
PLACE_OP: Final[str] = "place"
REFRESH_OP: Final[str] = "refresh"
RELOAD_SYSTEM_INTERFACE_OP: Final[str] = "reload_system_interface"
# Read-only: answered with the inventory document (desktop contracts.md section 5.5).
INVENTORY_OPS: Final[frozenset[str]] = frozenset({"desktops", "list"})
WINDOW_OPS: Final[frozenset[str]] = frozenset(
    {FOCUS_OP, "minimize", "restore", "maximize", PLACE_OP, CLOSE_OP, NAVIGATE_OP}
)
SHORTCUT_OPS: Final[frozenset[str]] = frozenset(
    {"shortcuts", "shortcut_set", "shortcut_move", "shortcut_remove", "wallpaper"}
)
# Ops that change what is on screen without changing the files: they reach the browser as a ``layout_op``
# message, as does a ``show`` that lands on a pulled-out window.
TRANSIENT_OPS: Final[frozenset[str]] = frozenset({REFRESH_OP, RELOAD_SYSTEM_INTERFACE_OP})
KNOWN_OPS: Final[frozenset[str]] = (
    frozenset({CONTEXT_OP, LOAD_OP, SHOW_OP, OPEN_OP}) | INVENTORY_OPS | WINDOW_OPS | SHORTCUT_OPS | TRANSIENT_OPS
)

# The status the op route refuses an op with when it would change where a window the target client has popped out
# into its own window sits, and the op did not carry ``force`` (plan-popped-out-layout-ops.md).
POPPED_OUT_REFUSAL_STATUS: Final[int] = 423

# The one non-id a window argument accepts: the requester's own window, which the op's ``requester`` names.
SELF_WINDOW: Final[str] = "self"
# The requester's app's pinned window on the target desktop (pinned-taskbar-entries plan section 4.8).
PINNED_WINDOW: Final[str] = "pinned"

# The keys that pick an op's target rather than describe the op; stripped before the op's own arguments are read.
CLIENT_ARG_KEY: Final[str] = "client"
DESKTOP_ARG_KEY: Final[str] = "desktop"
TARGET_ARG_KEYS: Final[frozenset[str]] = frozenset({CLIENT_ARG_KEY, DESKTOP_ARG_KEY})


@pure
def is_known_op(op: str) -> bool:
    return op in KNOWN_OPS


@pure
def op_only_args(args_raw: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in args_raw.items() if key not in TARGET_ARG_KEYS}


class OpRequester(FrozenModel):
    """Who posted an op: the app whose agent asked, and the marker (a chat id, an agent id) its window's path carries."""

    app: AppName = Field(description="The requesting app")
    marker: str = Field(description="The requester's marker; empty for a bare app")


@pure
def parse_op_requester(raw: Any) -> OpRequester | None:
    """The requester an op body carries: an ``{app, marker}`` object, or nothing (None or ""). Raises
    InvalidLayoutValueError for anything else: dropping a malformed requester would silently cost the op its
    attribution."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict) and isinstance(raw.get("app"), str):
        raw_marker = raw.get("marker")
        marker = "" if raw_marker is None else raw_marker
        if not isinstance(marker, str):
            raise InvalidLayoutValueError("``requester.marker`` must be a string")
        try:
            app = AppName(raw["app"])
        except ValueError as e:
            raise InvalidLayoutValueError(f"``requester.app`` {raw['app']!r} is not an app name: {e}") from e
        return OpRequester(app=app, marker=marker)
    raise InvalidLayoutValueError("``requester`` must be null or an object with ``app`` and ``marker``")


class Wallpaper(FrozenModel):
    """A reference to a wallpaper image: bundled with the shell, or a file in the workspace's wallpapers directory."""

    kind: WallpaperKind = Field(description="Bundled or file")
    name: WallpaperName = Field(description="The file name without its extension")


class DesktopOpArguments(FrozenModel):
    """The arguments of an op, as desktop contracts.md section 8 spells them (the target keys stripped)."""

    window: str = Field(default="", description="A window id, ``self``, or an app name")
    app: str = Field(default="", description="The app an ``open``, a ``show``, or a whole-app ``refresh`` names")
    path: str = Field(default="", description="The path an ``open``, a ``navigate``, or a ``show`` names")
    showing: tuple[str, ...] = Field(
        default=(), description="The other paths that count as already showing a ``show``'s path"
    )
    repoint: tuple[str, ...] = Field(
        default=(), description="The pages, without a query string, whose windows a ``show`` may point at its path"
    )
    launch: LaunchPathId | None = Field(
        default=None,
        description="The launch path an ``open`` runs (None for the app's default) or a shortcut op names",
    )
    params: dict[str, str] = Field(
        default_factory=dict,
        description="The launch path's params: a GET launch path's query, a POST launch path's body",
    )
    if_present: IfPresent = Field(
        default=IfPresent.FOCUS, description="Focus a window already at the path, or open another"
    )
    minimized: bool = Field(
        default=False,
        description="Whether an ``open`` places the window minimized for the target client; a window it finds instead "
        "is left as placed",
    )
    beside: str = Field(
        default="",
        description="A window an ``open`` lays its window beside for the target client: that one snapped to the left "
        "half, the opened one to the right half and on top",
    )
    zone: str = Field(default="", description="``left``, ``right``, or ``maximized`` for ``place``")
    frame: str = Field(default="", description="``x,y,width,height`` in fractions for ``place``")
    mode: ShortcutMode = Field(default=ShortcutMode.FOCUS, description="A shortcut's mode for ``shortcut_set``")
    cell: str = Field(default="", description="``column,row`` for ``shortcut_set`` and ``shortcut_move``")
    wallpaper: Wallpaper | None = Field(default=None, description="The wallpaper reference for ``wallpaper``")
    force: bool = Field(
        default=False,
        description="Apply an op that would bring a window the target client has popped out back onto the desktop, "
        "rather than being refused; ignored by an op that refuses nothing",
    )


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


class ShowRequest(FrozenModel):
    """A ``show`` of one of an app's paths on one client's screen."""

    app: AppName = Field(description="The app whose page to show")
    path: str = Field(description="The path to put on the client's screen")
    showing: tuple[str, ...] = Field(description="The app's other paths that count as already showing it")
    repoint: tuple[str, ...] = Field(
        description="The pages (paths without a query string) whose windows the shell may point at the path"
    )
    client_id: ClientId | None = Field(
        description="The client whose screen it goes on; None leaves it to the shell (the requester's, else the one "
        "connected client)"
    )


class OpenRequest(FrozenModel):
    """An ``open`` of an app's window at a path."""

    app: AppName = Field(description="The app whose window to open")
    path: str = Field(description="The page to open, a path under the app's origin")
    if_present: IfPresent = Field(description="Focus a window already at the path, or open another")
    is_minimized: bool = Field(description="Whether a window this open creates is placed minimized")
    client_id: ClientId | None = Field(description="The client it opens for; None leaves it to the shell")
    desktop: str | None = Field(description="The desktop, by name or id; None for the client's active one")


class WindowRequest(FrozenModel):
    """An op on one window (``focus``, ``close``, ``refresh``) for one client."""

    window: str = Field(description="A window id, ``self``, ``pinned``, or an app name")
    client_id: ClientId | None = Field(description="The client the op targets; None leaves it to the shell")
    desktop: str | None = Field(description="The desktop, by name or id; None for the client's active one")


class NavigateRequest(WindowRequest):
    """A ``navigate`` of one window to another path under its app."""

    path: str = Field(description="The path to point the window at")


class PlaceRequest(WindowRequest):
    """A ``place`` of one window at a frame."""

    frame: str = Field(description="``x,y,width,height`` in fractions of the backdrop")
    is_forced: bool = Field(
        default=False,
        description="Whether to place a window the client has popped out into its own window anyway, bringing it "
        "back onto the desktop; without it the shell refuses (WindowPoppedOutError)",
    )


@pure
def op_request_body(op: str, arguments: Mapping[str, Any], requester: OpRequester | None) -> dict[str, Any]:
    """The op route's body: the op, its arguments, and who asked."""
    return {
        "op": op,
        "args": dict(arguments),
        "requester": None if requester is None else requester.model_dump(mode="json"),
    }


@pure
def _with_target(arguments: DesktopOpArguments, client_id: ClientId | None, desktop: str | None) -> dict[str, Any]:
    """The arguments as the wire spells them (only the fields the caller set), with the target keys it names."""
    wire = arguments.model_dump(mode="json", exclude_unset=True)
    if client_id is not None:
        wire[CLIENT_ARG_KEY] = str(client_id)
    if desktop is not None:
        wire[DESKTOP_ARG_KEY] = desktop
    return wire


@pure
def show_op_arguments(request: ShowRequest) -> dict[str, Any]:
    arguments = DesktopOpArguments(
        app=str(request.app), path=request.path, showing=request.showing, repoint=request.repoint
    )
    return _with_target(arguments, request.client_id, None)


@pure
def open_op_arguments(request: OpenRequest) -> dict[str, Any]:
    arguments = DesktopOpArguments(
        app=str(request.app), path=request.path, if_present=request.if_present, minimized=request.is_minimized
    )
    return _with_target(arguments, request.client_id, request.desktop)


@pure
def window_op_arguments(request: WindowRequest) -> dict[str, Any]:
    return _with_target(DesktopOpArguments(window=request.window), request.client_id, request.desktop)


@pure
def navigate_op_arguments(request: NavigateRequest) -> dict[str, Any]:
    arguments = DesktopOpArguments(window=request.window, path=request.path)
    return _with_target(arguments, request.client_id, request.desktop)


@pure
def place_op_arguments(request: PlaceRequest) -> dict[str, Any]:
    if request.is_forced:
        arguments = DesktopOpArguments(window=request.window, frame=request.frame, force=True)
    else:
        arguments = DesktopOpArguments(window=request.window, frame=request.frame)
    return _with_target(arguments, request.client_id, request.desktop)
