"""Server-side support for the agent-driven layout surface, over addresses.

``system/scripts/layout.py`` posts ``{op, args, requester}`` to ``POST /api/layout/broadcast``
(``routes.py``): the read ops (``inspect``, ``context``) are answered from the state files and the
client-activity log, ``load`` switches a client's view, the document ops are applied by the shell to
the target client's layout file (``dockview_document.py``), and the transient ops are sent to that
client's windows. The script's ``list`` and ``views`` read ``GET /api/inventory`` instead. This module
holds the op tables, the op arguments, and the pure summary ``inspect`` answers with.
"""

from collections.abc import Mapping
from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import LayoutRecord
from imbue.system_interface.shell.data_types import instance_panel_params_by_id
from imbue.system_interface.shell.dockview_document import DEFAULT_SPLIT_RATIO
from imbue.system_interface.shell.dockview_document import Direction

# The ops the endpoint dispatches on. Anything else is a 400.
READ_OPS: Final[frozenset[str]] = frozenset({"inspect", "context"})
LOAD_OP: Final[str] = "load"
# Ops the shell applies to the target client's layout file (the file is the truth of the arrangement).
DOCUMENT_OPS: Final[frozenset[str]] = frozenset({"open", "focus", "split", "close", "move"})
# Ops that change what is on screen without changing the saved document: they alone reach the
# browser as a ``layout_op`` message.
TRANSIENT_OPS: Final[frozenset[str]] = frozenset({"maximize", "restore", "refresh", "reload_system_interface"})
KNOWN_OPS: Final[frozenset[str]] = READ_OPS | {LOAD_OP} | DOCUMENT_OPS | TRANSIENT_OPS

# Ops that name an instance or an app in ``args.address``.
ADDRESSED_OPS: Final[frozenset[str]] = frozenset({"open", "focus", "split", "close", "move", "maximize", "refresh"})

# Ops that dock a panel, and may therefore create the instance it shows.
CREATING_OPS: Final[frozenset[str]] = frozenset({"open", "split"})

# The one non-address an addressed op accepts: the requester's own instance, which the op's
# ``requester`` names.
SELF_ADDRESS: Final[str] = "self"


@pure
def is_known_op(op: str) -> bool:
    return op in KNOWN_OPS


@pure
def is_document_op(op: str) -> bool:
    return op in DOCUMENT_OPS


@pure
def is_transient_op(op: str) -> bool:
    return op in TRANSIENT_OPS


@pure
def is_addressed_op(op: str) -> bool:
    return op in ADDRESSED_OPS


@pure
def is_creating_op(op: str) -> bool:
    return op in CREATING_OPS


class DocumentOpArguments(FrozenModel):
    """The arguments of a document op, as ``layout.py`` posts them (contracts.md section 12)."""

    address: str = Field(
        default="", description="The instance or app the op names; ``self`` for the requester's own instance"
    )
    relative_to: str = Field(default=SELF_ADDRESS, description="The anchor of a split or a move")
    direction: Direction = Field(default=Direction.RIGHT, description="Where a split or a move lands")
    ratio: float = Field(default=DEFAULT_SPLIT_RATIO, description="The share of the anchor a split takes")
    new_group: bool = Field(default=False, description="Split even when a group already lies in the direction")
    action: str = Field(default="", description="The action a create runs; empty for the app's primary action")
    params: dict[str, str] = Field(default_factory=dict, description="The create's params")


@pure
def _orthogonal_orientation(orientation: str) -> str:
    return "VERTICAL" if orientation == "HORIZONTAL" else "HORIZONTAL"


@pure
def _serialize_grid_node(
    node: dict[str, Any],
    panel_by_id: Mapping[str, dict[str, Any]],
    orientation: str,
) -> dict[str, Any]:
    """Project the dockview grid tree into a compact summary; nested branches alternate orientation."""
    if node.get("type") == "leaf":
        data = node.get("data", {}) or {}
        active_view = data.get("activeView")
        panels = [
            {
                **panel_by_id.get(panel_id, {"address": None, "tab_id": None, "title": None}),
                "active": panel_id == active_view,
            }
            for panel_id in list(data.get("views", []) or [])
        ]
        return {"type": "leaf", "size_ratio": data.get("size"), "panels": panels}
    children = node.get("data", []) or []
    return {
        "type": "branch",
        "arrangement": "row" if orientation == "HORIZONTAL" else "column",
        "size_ratio": node.get("size"),
        "children": [
            _serialize_grid_node(child, panel_by_id, _orthogonal_orientation(orientation)) for child in children
        ],
    }


@pure
def layout_inspect(layout: LayoutRecord | None, title_by_address: Mapping[str, str]) -> dict[str, Any]:
    """A client's arrangement as the ``inspect`` op reports it: the panels with their addresses, and the grid tree."""
    if layout is None or layout.dockview is None:
        return {"active_panel": None, "panels": [], "tree": None}
    panel_by_id = {
        panel_id: {
            "address": str(params.address),
            "tab_id": str(params.tab_id),
            "title": title_by_address.get(str(params.address)),
        }
        for panel_id, params in instance_panel_params_by_id(layout.dockview).items()
    }
    dockview = layout.dockview
    grid = dockview.get("grid", {}) or {}
    root = grid.get("root")
    tree = (
        _serialize_grid_node(root, panel_by_id, grid.get("orientation") or "HORIZONTAL")
        if isinstance(root, dict)
        else None
    )
    return {
        "active_panel": dockview.get("activeGroup"),
        "panels": [
            panel_by_id[panel_id] for panel_id in (dockview.get("panels", {}) or {}) if panel_id in panel_by_id
        ],
        "tree": tree,
    }
