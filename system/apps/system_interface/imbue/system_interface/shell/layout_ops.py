"""Server-side support for the agent-driven layout surface, over addresses.

``system/scripts/layout.py`` posts ``{op, args, agent_id}`` to ``POST /api/layout/broadcast``
(``routes.py``): the read ops (``list``, ``inspect``, ``views``, ``context``) are answered from
the inventory and the state files, ``load`` switches a client's view, and every other op is
broadcast to the connected clients of the target view, which apply it to their dock. This
module holds the op tables, the advisory mutex, and the pure summaries the read ops answer with.
"""

import threading
import time
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.data_types import LayoutRecord
from imbue.system_interface.shell.data_types import Project
from imbue.system_interface.shell.data_types import action_wire_json
from imbue.system_interface.shell.data_types import effective_actions
from imbue.system_interface.shell.layouts import StoredLayout
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import EVERYTHING_VIEW_ID
from imbue.system_interface.shell.primitives import EVERYTHING_VIEW_NAME

# The ops the endpoint dispatches on. Anything else is a 400.
KNOWN_OPS: Final[frozenset[str]] = frozenset(
    {
        "list",
        "inspect",
        "open",
        "focus",
        "split",
        "close",
        "move",
        "maximize",
        "restore",
        "refresh",
        "reload_system_interface",
        "context",
        "load",
        "views",
    }
)

# Ops that mutate a client's arrangement and therefore acquire the mutex. ``focus`` is here
# because dockview persists the active panel, so a focus changes the next save's bytes.
MUTATING_OPS: Final[frozenset[str]] = frozenset({"open", "focus", "split", "close", "move", "maximize", "restore"})

# Ops that reach the frontend as a ``layout_op`` message.
BROADCASTING_OPS: Final[frozenset[str]] = frozenset(
    {"open", "focus", "split", "close", "move", "maximize", "restore", "refresh", "reload_system_interface"}
)

# Ops that name an instance or an app in ``args.address``.
ADDRESSED_OPS: Final[frozenset[str]] = frozenset({"open", "focus", "split", "close", "move", "maximize", "refresh"})

# The one non-address an addressed op accepts: the requester's own chat, which the connected
# client resolves from the op's ``requester_agent_id`` (``layout.py`` passes it through).
SELF_ADDRESS: Final[str] = "self"

# The mutex TTL: comfortably longer than one dockview mutation round trip, short enough that
# a wedged op cannot lock the workspace for an annoying length of time.
_MUTEX_TTL_SECONDS: Final[float] = 0.5


@pure
def is_known_op(op: str) -> bool:
    return op in KNOWN_OPS


@pure
def is_mutating_op(op: str) -> bool:
    return op in MUTATING_OPS


@pure
def is_broadcasting_op(op: str) -> bool:
    return op in BROADCASTING_OPS


@pure
def is_addressed_op(op: str) -> bool:
    return op in ADDRESSED_OPS


class LayoutMutex(MutableModel):
    """Advisory in-process mutex protecting layout-mutating ops.

    Acquisition is non-blocking and TTL-bounded: a holder that does not release is auto-released
    after the TTL. Conflicting requests fail at once with HTTP 409 and the in-flight op's
    metadata so the caller can pick its own retry strategy.
    """

    ttl_seconds: float = Field(default=_MUTEX_TTL_SECONDS, description="How long a holder keeps the mutex unreleased")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _holder: dict[str, Any] | None = PrivateAttr(default=None)

    def try_acquire(self, agent_id: str, op: str, args: Mapping[str, Any]) -> dict[str, Any] | None:
        """Take the mutex; None on success, else the holder's description for a 409."""
        now = time.monotonic()
        with self._lock:
            if self._holder is not None and now - self._holder["started_at_monotonic"] < self.ttl_seconds:
                return {
                    "agent_id": self._holder["agent_id"],
                    "operation": self._holder["op"],
                    "args": self._holder["args"],
                    "started_at": self._holder["started_at_wall"],
                }
            self._holder = {
                "agent_id": agent_id,
                "op": op,
                "args": dict(args),
                "started_at_monotonic": now,
                "started_at_wall": time.time(),
            }
            return None

    def release(self, agent_id: str, op: str) -> None:
        """Best-effort release: a no-op when the slot was already taken over after the TTL."""
        with self._lock:
            holder = self._holder
            if holder is not None and holder["agent_id"] == agent_id and holder["op"] == op:
                self._holder = None

    def retry_after_ms(self) -> int:
        return int(self.ttl_seconds * 1000)


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
            "address": str(tab.address),
            "tab_id": str(tab.tab_id),
            "title": title_by_address.get(str(tab.address)),
        }
        for panel_id, tab in layout.tabs.items()
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


@pure
def layout_list(
    entries: Sequence[AppInventoryEntry],
    layouts: Sequence[StoredLayout],
) -> list[dict[str, Any]]:
    """Every app with its instances, statuses, and which clients dock each (the ``list`` op, contracts.md section 12)."""
    docked_in_by_address: dict[str, list[str]] = {}
    for stored in layouts:
        for tab in stored.layout.tabs.values():
            clients = docked_in_by_address.setdefault(str(tab.address), [])
            if str(stored.client_id) not in clients:
                clients.append(str(stored.client_id))
    listing: list[dict[str, Any]] = []
    for entry in entries:
        if entry.row.internal:
            continue
        instances = [
            {
                "key": instance.key,
                "address": str(entry.address_of(instance)),
                "title": instance.title,
                "status": instance.status.value,
                "docked_in": docked_in_by_address.get(str(entry.address_of(instance)), []),
            }
            for instance in entry.instances
        ]
        listing.append(
            {
                "name": str(entry.row.name),
                "display_name": str(entry.row.display_name)
                if entry.row.display_name is not None
                else str(entry.row.name),
                "is_running": entry.is_running,
                "actions": [action_wire_json(action) for action in effective_actions(entry.row)],
                "instances": instances,
            }
        )
    return listing


@pure
def layout_views(
    projects: Sequence[Project],
    everything_tabs: Sequence[Address],
    clients_by_view: Mapping[str, Sequence[dict[str, str]]],
) -> list[dict[str, Any]]:
    """Every view with its tab set and the clients on it (the ``views`` op)."""
    views = [
        {
            "id": str(project.id),
            "name": project.name,
            "is_everything": False,
            "tabs": [str(address) for address in project.tabs],
            "clients": list(clients_by_view.get(str(project.id), [])),
        }
        for project in projects
    ]
    views.append(
        {
            "id": EVERYTHING_VIEW_ID,
            "name": EVERYTHING_VIEW_NAME,
            "is_everything": True,
            "tabs": [str(address) for address in everything_tabs],
            "clients": list(clients_by_view.get(EVERYTHING_VIEW_ID, [])),
        }
    )
    return views


@pure
def view_display_name(view_id: str, projects: Sequence[Project]) -> str:
    if view_id == EVERYTHING_VIEW_ID:
        return EVERYTHING_VIEW_NAME
    for project in projects:
        if project.id == view_id:
            return project.name
    return view_id
