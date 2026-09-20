"""The op tables and arguments of the agent-facing op route (desktop contracts.md section 8).

``system/scripts/layout.py`` posts ``{op, args, requester}`` to ``POST /api/layout/broadcast``
(``routes.py``): ``context`` is answered from the client-activity log, the inventory ops from the
desktops, and every other op is applied by the shell to the desktop and the target client's
placements (``desktop_routes.py``), the two transient ops reaching the client's windows instead.
"""

from typing import Any
from typing import Final

from app_manifest.manifest import ShortcutMode
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import Wallpaper
from imbue.system_interface.shell.errors import LayoutOpError
from imbue.system_interface.shell.primitives import IfPresent

# The ops the endpoint dispatches on. Anything else is a 400.
CONTEXT_OP: Final[str] = "context"
LOAD_OP: Final[str] = "load"
# Read-only: answered with the desktops.
INVENTORY_OPS: Final[frozenset[str]] = frozenset({"desktops", "list"})
WINDOW_OPS: Final[frozenset[str]] = frozenset({"focus", "minimize", "restore", "maximize", "place", "close", "navigate"})
SHORTCUT_OPS: Final[frozenset[str]] = frozenset(
    {"shortcuts", "shortcut_set", "shortcut_move", "shortcut_remove", "wallpaper"}
)
# Ops that change what is on screen without changing the files: they alone reach the browser as a
# ``layout_op`` message.
TRANSIENT_OPS: Final[frozenset[str]] = frozenset({"refresh", "reload_system_interface"})
KNOWN_OPS: Final[frozenset[str]] = (
    frozenset({CONTEXT_OP, LOAD_OP, "open"}) | INVENTORY_OPS | WINDOW_OPS | SHORTCUT_OPS | TRANSIENT_OPS
)

# The one non-id a window argument accepts: the requester's own window, which the op's ``requester`` names.
SELF_WINDOW: Final[str] = "self"


@pure
def is_known_op(op: str) -> bool:
    return op in KNOWN_OPS


class OpRequester(FrozenModel):
    """Who posted an op: the app whose agent asked, and the marker (a chat id, an agent id) its window's path carries."""

    app: AppName = Field(description="The requesting app")
    marker: str = Field(description="The requester's marker; empty for a bare app")


@pure
def parse_op_requester(raw: Any) -> OpRequester | None:
    """The requester an op body carries: an ``{app, marker}`` object, or nothing (None or ""). Raises LayoutOpError
    (a 400) for anything else: dropping a malformed requester would silently cost the op its attribution."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict) and isinstance(raw.get("app"), str):
        raw_marker = raw.get("marker")
        marker = "" if raw_marker is None else raw_marker
        if not isinstance(marker, str):
            raise LayoutOpError("``requester.marker`` must be a string")
        return OpRequester(app=AppName(raw["app"]), marker=marker)
    raise LayoutOpError("``requester`` must be null or an object with ``app`` and ``marker``")


class DesktopOpArguments(FrozenModel):
    """The arguments of an op, as desktop contracts.md section 8 spells them (the target keys stripped)."""

    window: str = Field(default="", description="A window id, ``self``, or an app name")
    app: str = Field(default="", description="The app an ``open`` or a whole-app ``refresh`` names")
    path: str = Field(default="", description="The path an ``open`` or a ``navigate`` names")
    launch: LaunchPathId | None = Field(
        default=None,
        description="The launch path an ``open`` runs (None for the app's default) or a shortcut op names",
    )
    params: dict[str, str] = Field(default_factory=dict, description="The launch path's query parameters")
    if_present: IfPresent = Field(
        default=IfPresent.FOCUS, description="Focus a window already at the path, or open another"
    )
    zone: str = Field(default="", description="``left``, ``right``, or ``maximized`` for ``place``")
    frame: str = Field(default="", description="``x,y,width,height`` in fractions for ``place``")
    mode: ShortcutMode = Field(default=ShortcutMode.FOCUS, description="A shortcut's mode for ``shortcut_set``")
    cell: str = Field(default="", description="``column,row`` for ``shortcut_set`` and ``shortcut_move``")
    wallpaper: Wallpaper | None = Field(default=None, description="The wallpaper reference for ``wallpaper``")
