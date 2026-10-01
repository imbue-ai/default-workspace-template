from collections.abc import Mapping
from typing import Any
from typing import Final
from typing import assert_never

from app_manifest.manifest import ShortcutMode
from app_manifest.manifest import describe_validation_error
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from pydantic import Field
from pydantic import ValidationError
from pydantic import field_validator

from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.primitives import ClientActivityKind
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import IfPresent
from workspace_layout.primitives import LayoutOp
from workspace_layout.primitives import SpecialWindow
from workspace_layout.primitives import WindowId
from workspace_layout.primitives import WindowPage
from workspace_layout.primitives import WindowPath
from workspace_layout.primitives import WindowState
from workspace_layout.records import Frame
from workspace_layout.records import GridCell
from workspace_layout.records import Wallpaper

# The keys that pick an op's target rather than describe the op; stripped before the op's own arguments are read.
CLIENT_ARG_KEY: Final[str] = "client"
DESKTOP_ARG_KEY: Final[str] = "desktop"
TARGET_ARG_KEYS: Final[frozenset[str]] = frozenset({CLIENT_ARG_KEY, DESKTOP_ARG_KEY})


@pure
def parse_layout_op(raw: Any) -> LayoutOp | None:
    """The op a body's ``op`` names, or None when it names none the op route knows (a 400)."""
    if not isinstance(raw, str):
        return None
    try:
        return LayoutOp(raw)
    except ValueError:
        return None


WindowReference = WindowId | SpecialWindow | AppName


@pure
def parse_window_reference(raw: str) -> WindowReference:
    """What a window argument names: a window id, ``self``, ``pinned``, or an app name (that app's most recently
    focused window); raises InvalidLayoutValueError for anything else."""
    if not raw:
        raise InvalidLayoutValueError("this op needs a window: a window id, 'self', 'pinned', or an app name")
    try:
        return SpecialWindow(raw)
    except ValueError:
        pass
    try:
        return WindowId(raw)
    except InvalidLayoutValueError:
        pass
    try:
        return AppName(raw)
    except ValueError as e:
        raise InvalidLayoutValueError(
            f"{raw!r} is not a window: give a window id (win-<hex>), 'self', 'pinned', or an app name"
        ) from e


@pure
def op_only_args(args_raw: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in args_raw.items() if key not in TARGET_ARG_KEYS}


class OpRequester(FrozenModel):
    """Who posted an op: the app whose agent asked, and the marker (a chat id, an agent id) its window's path carries."""

    app: AppName = Field(description="The requesting app")
    marker: str = Field(description="The requester's marker; empty for a bare app")


@pure
def requester_spelling(requester: OpRequester | None) -> str:
    """The requester as a ``layout_op`` message carries it: ``<app>`` or ``<app>:<marker>``, empty for none."""
    if requester is None:
        return ""
    return str(requester.app) + (f":{requester.marker}" if requester.marker else "")


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


# The states ``place`` sets a window to; a window comes back to ``NORMAL`` through ``restore`` or a ``frame``.
PLACEABLE_STATES: Final[tuple[WindowState, ...]] = (
    WindowState.SNAPPED_LEFT,
    WindowState.SNAPPED_RIGHT,
    WindowState.MAXIMIZED,
)


class DesktopOpArguments(FrozenModel):
    """The arguments of an op, as desktop contracts.md section 8 spells them (the target keys stripped)."""

    window: str = Field(default="", description="A window id, ``self``, ``pinned``, or an app name")
    app: AppName | None = Field(
        default=None, description="The app an ``open``, a ``show``, a shortcut op, or a whole-app ``refresh`` names"
    )
    path: WindowPath | None = Field(
        default=None, description="The path an ``open``, a ``navigate``, or a ``show`` names"
    )
    showing: tuple[WindowPath, ...] = Field(
        default=(), description="The other paths that count as already showing a ``show``'s path"
    )
    repoint: tuple[WindowPage, ...] = Field(
        default=(), description="The pages whose windows a ``show`` may point at its path"
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
    state: WindowState | None = Field(default=None, description="The state ``place`` sets, one of PLACEABLE_STATES")
    frame: Frame | None = Field(default=None, description="The frame ``place`` sets")
    mode: ShortcutMode = Field(default=ShortcutMode.FOCUS, description="A shortcut's mode for ``shortcut_set``")
    cell: GridCell | None = Field(default=None, description="The cell for ``shortcut_set`` and ``shortcut_move``")
    wallpaper: Wallpaper | None = Field(default=None, description="The wallpaper reference for ``wallpaper``")

    @field_validator("state")
    @classmethod
    def _check_placeable(cls, state: WindowState | None) -> WindowState | None:
        if state is not None and state not in PLACEABLE_STATES:
            raise InvalidLayoutValueError(
                f"place sets a state of {[placeable.value for placeable in PLACEABLE_STATES]}, not {state.value!r}; "
                "a frame or a restore puts a window back to normal"
            )
        return state


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
    path: WindowPath = Field(description="The path to put on the client's screen")
    showing: tuple[WindowPath, ...] = Field(description="The app's other paths that count as already showing it")
    repoint: tuple[WindowPage, ...] = Field(description="The pages whose windows the shell may point at the path")
    client_id: ClientId | None = Field(
        description="The client whose screen it goes on; None leaves it to the shell (the requester's, else the one "
        "connected client)"
    )


class OpenRequest(FrozenModel):
    """An ``open`` of an app's window at a path."""

    app: AppName = Field(description="The app whose window to open")
    path: WindowPath = Field(description="The page to open, a path under the app's origin")
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

    path: WindowPath = Field(description="The path to point the window at")


class PlaceRequest(WindowRequest):
    """A ``place`` of one window at a frame."""

    frame: Frame = Field(description="The frame, in fractions of the backdrop")


class OpBody(FrozenModel):
    """The op route's body (desktop contracts.md section 8): the op, its arguments, and who asked."""

    op: LayoutOp = Field(description="The op")
    args: dict[str, Any] = Field(
        description="The op's arguments, the target keys among them; read as DesktopOpArguments once they are stripped"
    )
    requester: OpRequester | None = Field(description="Who asked; None for a caller outside an agent")


@pure
def op_request_body(op: LayoutOp, arguments: Mapping[str, Any], requester: OpRequester | None) -> dict[str, Any]:
    """The op route's body as the wire spells it."""
    return OpBody(op=op, args=dict(arguments), requester=requester).model_dump(mode="json")


@pure
def parse_op_body(raw: Any) -> OpBody:
    """The op route's body as the shell reads one; raises InvalidLayoutValueError naming what is wrong with it."""
    if not isinstance(raw, dict):
        raise InvalidLayoutValueError("Request body must be a JSON object")
    op = parse_layout_op(raw.get("op"))
    if op is None:
        raise InvalidLayoutValueError(f"Unknown layout op: {raw.get('op')!r}")
    requester = parse_op_requester(raw.get("requester"))
    args = raw.get("args", {})
    if not isinstance(args, dict):
        raise InvalidLayoutValueError("``args`` must be a JSON object")
    return OpBody(op=op, args=args, requester=requester)


@pure
def read_op_arguments(args: Mapping[str, Any]) -> DesktopOpArguments:
    """An op's arguments as the shell reads them: the target keys strings, the rest ones ``DesktopOpArguments``
    takes; raises InvalidLayoutValueError naming what is wrong."""
    for key in TARGET_ARG_KEYS & args.keys():
        if not isinstance(args[key], str):
            raise InvalidLayoutValueError(f"``args.{key}`` must be a string")
    try:
        return DesktopOpArguments.model_validate(op_only_args(args))
    except ValidationError as e:
        raise InvalidLayoutValueError(f"bad op arguments: {describe_validation_error(e)}") from e


@pure
def op_reads_arguments(op: LayoutOp) -> bool:
    """Whether the shell reads an op's arguments at all: ``context`` and the inventory ops are answered whatever they
    carry, and every other op is refused for arguments ``DesktopOpArguments`` does not take."""
    match op:
        case LayoutOp.CONTEXT | LayoutOp.DESKTOPS | LayoutOp.LIST:
            return False
        case (
            LayoutOp.LOAD
            | LayoutOp.OPEN
            | LayoutOp.SHOW
            | LayoutOp.FOCUS
            | LayoutOp.MINIMIZE
            | LayoutOp.RESTORE
            | LayoutOp.MAXIMIZE
            | LayoutOp.PLACE
            | LayoutOp.CLOSE
            | LayoutOp.NAVIGATE
            | LayoutOp.SHORTCUTS
            | LayoutOp.SHORTCUT_SET
            | LayoutOp.SHORTCUT_MOVE
            | LayoutOp.SHORTCUT_REMOVE
            | LayoutOp.WALLPAPER
            | LayoutOp.REFRESH
            | LayoutOp.RELOAD_SYSTEM_INTERFACE
        ):
            return True
        case _:
            assert_never(op)


@pure
def wire_arguments(arguments: DesktopOpArguments, client_id: ClientId | None, desktop: str | None) -> dict[str, Any]:
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
        app=request.app, path=request.path, showing=request.showing, repoint=request.repoint
    )
    return wire_arguments(arguments, request.client_id, None)


@pure
def open_op_arguments(request: OpenRequest) -> dict[str, Any]:
    arguments = DesktopOpArguments(
        app=request.app, path=request.path, if_present=request.if_present, minimized=request.is_minimized
    )
    return wire_arguments(arguments, request.client_id, request.desktop)


@pure
def window_op_arguments(request: WindowRequest) -> dict[str, Any]:
    return wire_arguments(DesktopOpArguments(window=request.window), request.client_id, request.desktop)


@pure
def navigate_op_arguments(request: NavigateRequest) -> dict[str, Any]:
    arguments = DesktopOpArguments(window=request.window, path=request.path)
    return wire_arguments(arguments, request.client_id, request.desktop)


@pure
def place_op_arguments(request: PlaceRequest) -> dict[str, Any]:
    arguments = DesktopOpArguments(window=request.window, frame=request.frame)
    return wire_arguments(arguments, request.client_id, request.desktop)
