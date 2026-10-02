from typing import Annotated
from typing import Any
from typing import Final
from typing import Literal
from typing import assert_never

from app_manifest.manifest import ShortcutMode
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from pydantic import AfterValidator
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError
from pydantic import model_validator
from pydantic_core import ErrorDetails

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


def _check_window_spelling(raw: str) -> str:
    parse_window_reference(raw)
    return raw


# A window argument as the wire carries it, refused when it spells no window.
WindowSpelling = Annotated[str, AfterValidator(_check_window_spelling)]


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


# Each op's arguments (desktop contracts.md section 8), as the body's ``args`` spells them


class OpTarget(FrozenModel):
    """The keys that pick an op's target: the client whose desktop it edits, and the desktop."""

    client: ClientId | None = Field(
        default=None,
        description="The client the op targets; None leaves it to the shell (the requester's, else the one connected "
        "client)",
    )
    desktop: str | None = Field(
        default=None,
        min_length=1,
        description="The desktop, by name or id; None for the client's active one. Naming another edits that one and "
        "switches the client to it",
    )


class UnreadArgs(FrozenModel):
    """The arguments of an op the shell answers whatever it carries."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class NoArgs(FrozenModel):
    """The arguments of an op that takes none."""


class LoadArgs(OpTarget):
    """``load``: switch the target client to a desktop."""

    desktop: str = Field(min_length=1, description="The desktop to switch the client to, by name or id")


class ShowArgs(OpTarget):
    """``show``: put a path of an app on the target client's screen, the shell choosing the window."""

    app: AppName = Field(description="The app whose page to show")
    path: WindowPath = Field(description="The path to put on the client's screen")
    showing: tuple[WindowPath, ...] = Field(
        default=(), description="The app's other paths that count as already showing it"
    )
    repoint: tuple[WindowPage, ...] = Field(
        default=(), description="The pages whose windows the shell may point at the path"
    )


class OpenArgs(OpTarget):
    """``open``: open (or focus) a window of an app at a path, or at the page of one of its launch paths."""

    app: AppName = Field(description="The app whose window to open")
    path: WindowPath | None = Field(default=None, description="The page to open; None to open a launch path's")
    launch: LaunchPathId | None = Field(
        default=None, description="The launch path to open (None for the app's default) when no path is named"
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
        description="Whether a window this open creates is placed minimized for the target client; a window it finds "
        "instead is left as placed",
    )
    beside: WindowSpelling | None = Field(
        default=None,
        description="A window to lay the opened one beside for the target client: that one snapped to the left half, "
        "the opened one to the right half and on top",
    )

    @model_validator(mode="after")
    def _check_one_page_and_one_place(self) -> "OpenArgs":
        if self.path is not None and (self.launch is not None or self.params):
            raise InvalidLayoutValueError("an open names a path or a launch path, not both")
        if self.minimized and self.beside is not None:
            raise InvalidLayoutValueError(
                "an open puts the window out of sight with minimized or beside another window, not both"
            )
        return self


class _WindowTarget(OpTarget):
    """The keys of an op on one window: its target, and the window."""

    window: WindowSpelling = Field(description="A window id, ``self``, ``pinned``, or an app name")


class WindowArgs(_WindowTarget):
    """An op on one window that takes nothing else."""


class PlaceArgs(_WindowTarget):
    """``place``: set a window's state, or its frame with the state ``NORMAL``."""

    state: WindowState | None = Field(default=None, description="The state to set, one of PLACEABLE_STATES")
    frame: Frame | None = Field(default=None, description="The frame to set")

    @model_validator(mode="after")
    def _check_a_state_or_a_frame(self) -> "PlaceArgs":
        if (self.state is None) == (self.frame is None):
            raise InvalidLayoutValueError(
                f"place takes a state (one of {[placeable.value for placeable in PLACEABLE_STATES]}) or a frame, "
                "not both or neither"
            )
        if self.state is not None and self.state not in PLACEABLE_STATES:
            raise InvalidLayoutValueError(
                f"place sets a state of {[placeable.value for placeable in PLACEABLE_STATES]}, not "
                f"{self.state.value!r}; a frame or a restore puts a window back to normal"
            )
        return self


class NavigateArgs(_WindowTarget):
    """``navigate``: point a window at another path under its app."""

    path: WindowPath = Field(description="The path to point the window at")


class RefreshWindowArgs(_WindowTarget):
    """``refresh`` of one window: reload its page on the target client."""


class RefreshAppArgs(FrozenModel):
    """``refresh`` of an app: reload every page of it on every client, so it names no client or desktop."""

    app: AppName = Field(description="The app every page of which to reload")


class ShortcutsArgs(OpTarget):
    """``shortcuts``: answer the desktop's shortcuts."""


class _ShortcutTarget(OpTarget):
    """What every shortcut write names: its target, and the shortcut's app and launch path."""

    app: AppName = Field(description="The shortcut's app")
    launch: LaunchPathId = Field(description="The launch path the shortcut runs")


class ShortcutArgs(_ShortcutTarget):
    """``shortcut_remove``: remove a shortcut from the desktop."""


class ShortcutSetArgs(_ShortcutTarget):
    """``shortcut_set``: add a shortcut to the desktop, or change its mode or cell."""

    mode: ShortcutMode = Field(
        default=ShortcutMode.FOCUS, description="Focus the app's latest window, or open a new one"
    )
    cell: GridCell | None = Field(default=None, description="The cell; None for the next free one")


class ShortcutMoveArgs(_ShortcutTarget):
    """``shortcut_move``: move a shortcut to another cell."""

    cell: GridCell = Field(description="The cell to move it to")


class WallpaperArgs(OpTarget):
    """``wallpaper``: set or clear the desktop's wallpaper."""

    wallpaper: Wallpaper | None = Field(description="The wallpaper to set; None for the theme's default")


# The op route's bodies (desktop contracts.md section 8): one per op, or per group of ops that take the same arguments

# The window ops that take only the window.
WindowOp = Literal[LayoutOp.FOCUS, LayoutOp.MINIMIZE, LayoutOp.RESTORE, LayoutOp.MAXIMIZE, LayoutOp.CLOSE]


class _OpBodyBase(FrozenModel):
    requester: OpRequester | None = Field(default=None, description="Who asked; None for a caller outside an agent")


class ContextBody(_OpBodyBase):
    op: Literal[LayoutOp.CONTEXT] = LayoutOp.CONTEXT
    args: UnreadArgs = Field(default_factory=UnreadArgs, description="Unread")


class InventoryBody(_OpBodyBase):
    op: Literal[LayoutOp.DESKTOPS, LayoutOp.LIST] = Field(description="desktops or list; both answer the inventory")
    args: UnreadArgs = Field(default_factory=UnreadArgs, description="Unread")


class LoadBody(_OpBodyBase):
    op: Literal[LayoutOp.LOAD] = LayoutOp.LOAD
    args: LoadArgs = Field(description="The desktop to switch to")


class ShowBody(_OpBodyBase):
    op: Literal[LayoutOp.SHOW] = LayoutOp.SHOW
    args: ShowArgs = Field(description="The page to show")


class OpenBody(_OpBodyBase):
    op: Literal[LayoutOp.OPEN] = LayoutOp.OPEN
    args: OpenArgs = Field(description="The window to open")


class WindowOpBody(_OpBodyBase):
    op: WindowOp = Field(description="The window op")
    args: WindowArgs = Field(description="The window")


class PlaceBody(_OpBodyBase):
    op: Literal[LayoutOp.PLACE] = LayoutOp.PLACE
    args: PlaceArgs = Field(description="The window and where it goes")


class NavigateBody(_OpBodyBase):
    op: Literal[LayoutOp.NAVIGATE] = LayoutOp.NAVIGATE
    args: NavigateArgs = Field(description="The window and its new path")


RefreshArgs = RefreshWindowArgs | RefreshAppArgs


class RefreshWindowBody(_OpBodyBase):
    op: Literal[LayoutOp.REFRESH] = LayoutOp.REFRESH
    args: RefreshWindowArgs = Field(description="The window")


class RefreshAppBody(_OpBodyBase):
    op: Literal[LayoutOp.REFRESH] = LayoutOp.REFRESH
    args: RefreshAppArgs = Field(description="The app")


class ReloadSystemInterfaceBody(_OpBodyBase):
    op: Literal[LayoutOp.RELOAD_SYSTEM_INTERFACE] = LayoutOp.RELOAD_SYSTEM_INTERFACE
    args: NoArgs = Field(default_factory=NoArgs, description="None")


class ShortcutsBody(_OpBodyBase):
    op: Literal[LayoutOp.SHORTCUTS] = LayoutOp.SHORTCUTS
    args: ShortcutsArgs = Field(default_factory=ShortcutsArgs, description="The desktop whose shortcuts to answer")


class ShortcutSetBody(_OpBodyBase):
    op: Literal[LayoutOp.SHORTCUT_SET] = LayoutOp.SHORTCUT_SET
    args: ShortcutSetArgs = Field(description="The shortcut")


class ShortcutMoveBody(_OpBodyBase):
    op: Literal[LayoutOp.SHORTCUT_MOVE] = LayoutOp.SHORTCUT_MOVE
    args: ShortcutMoveArgs = Field(description="The shortcut and its new cell")


class ShortcutRemoveBody(_OpBodyBase):
    op: Literal[LayoutOp.SHORTCUT_REMOVE] = LayoutOp.SHORTCUT_REMOVE
    args: ShortcutArgs = Field(description="The shortcut")


class WallpaperBody(_OpBodyBase):
    op: Literal[LayoutOp.WALLPAPER] = LayoutOp.WALLPAPER
    args: WallpaperArgs = Field(description="The wallpaper")


OpBody = (
    ContextBody
    | InventoryBody
    | LoadBody
    | ShowBody
    | OpenBody
    | WindowOpBody
    | PlaceBody
    | NavigateBody
    | RefreshWindowBody
    | RefreshAppBody
    | ReloadSystemInterfaceBody
    | ShortcutsBody
    | ShortcutSetBody
    | ShortcutMoveBody
    | ShortcutRemoveBody
    | WallpaperBody
)


def _body_model(op: LayoutOp, args: dict[str, Any]) -> type[OpBody]:
    """The body model an op is read with; a ``refresh`` is of an app when its arguments name one, else of a window."""
    match op:
        case LayoutOp.CONTEXT:
            return ContextBody
        case LayoutOp.DESKTOPS | LayoutOp.LIST:
            return InventoryBody
        case LayoutOp.LOAD:
            return LoadBody
        case LayoutOp.SHOW:
            return ShowBody
        case LayoutOp.OPEN:
            return OpenBody
        case LayoutOp.FOCUS | LayoutOp.MINIMIZE | LayoutOp.RESTORE | LayoutOp.MAXIMIZE | LayoutOp.CLOSE:
            return WindowOpBody
        case LayoutOp.PLACE:
            return PlaceBody
        case LayoutOp.NAVIGATE:
            return NavigateBody
        case LayoutOp.REFRESH:
            return RefreshAppBody if "app" in args else RefreshWindowBody
        case LayoutOp.RELOAD_SYSTEM_INTERFACE:
            return ReloadSystemInterfaceBody
        case LayoutOp.SHORTCUTS:
            return ShortcutsBody
        case LayoutOp.SHORTCUT_SET:
            return ShortcutSetBody
        case LayoutOp.SHORTCUT_MOVE:
            return ShortcutMoveBody
        case LayoutOp.SHORTCUT_REMOVE:
            return ShortcutRemoveBody
        case LayoutOp.WALLPAPER:
            return WallpaperBody
        case _:
            assert_never(op)


@pure
def _describe_argument_problem(error: ErrorDetails) -> str:
    """One refused argument: where it is in the body, and why, in the rule's own words for a rule of ours."""
    location = ".".join(str(part) for part in error["loc"]) or "args"
    reason = error.get("ctx", {}).get("error") if error["type"] == "value_error" else None
    return f"{location}: {reason if reason is not None else error['msg']}"


@pure
def op_request_body(body: OpBody) -> dict[str, Any]:
    """The op route's body as the wire spells it: the arguments the caller set, and who asked."""
    return {
        "op": body.op.value,
        "args": body.args.model_dump(mode="json", exclude_unset=True),
        "requester": None if body.requester is None else body.requester.model_dump(mode="json"),
    }


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
    try:
        return _body_model(op, args).model_validate({"op": op, "args": args, "requester": requester})
    except ValidationError as e:
        problems = "; ".join(_describe_argument_problem(error) for error in e.errors())
        raise InvalidLayoutValueError(f"bad {op} arguments: {problems}") from e
