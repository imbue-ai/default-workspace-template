import json
import queue
import threading
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from loguru import logger as _loguru_logger
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel

# Per-client buffer depth. Holds at most this many state-change broadcasts before
# the broadcaster starts dropping the oldest. State-change broadcasts are
# typically sub-Hz, so 1000 messages represents well over a minute of falling
# behind even under burst load.
_CLIENT_QUEUE_MAX_SIZE = 1000

# How many *consecutive* broadcasts a single client can be ``queue.Full`` for
# before the broadcaster gives up on it. A momentarily-slow client whose handler
# drains even one message between broadcasts resets the counter and stays
# connected. Only a client that makes zero progress over this many broadcasts
# gets disconnected.
_MAX_CONSECUTIVE_QUEUE_FULL = 50


def _drain_queue(client_queue: queue.Queue[str | None]) -> None:
    """Remove all pending items from ``client_queue`` so it ends up empty."""
    is_drained = False
    while not is_drained:
        try:
            client_queue.get_nowait()
        except queue.Empty:
            is_drained = True


class WebSocketBroadcaster(MutableModel):
    """Manages WebSocket clients and broadcasts state updates.

    Thread-safe: background threads call broadcast methods which put messages
    into per-client queues. Each WebSocket handler runs in its own thread and
    drains its queue. There is no asyncio anywhere -- a wedged client is freed
    either by flask-sock's ``ping_interval`` keepalive closing the dead socket,
    or by the broadcaster evicting it (draining its queue and pushing the
    shutdown sentinel so its handler thread unblocks and exits).
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _client_queues: list[queue.Queue[str | None]] = PrivateAttr(default_factory=list)
    # Number of consecutive broadcasts a given client's queue has been full for.
    # Keyed by ``id(queue)`` to avoid hashing the queue itself. Reset to 0 on any
    # successful enqueue. A client is only disconnected once its counter reaches
    # ``_MAX_CONSECUTIVE_QUEUE_FULL`` -- a brief stall is tolerated.
    _consecutive_queue_full_by_id: dict[int, int] = PrivateAttr(default_factory=dict)
    # Self-reported identity of each connected client (client_id, active view,
    # device kind), keyed by ``id(queue)``. Populated when the client sends its
    # ``client_state`` registration over the WebSocket; absent for clients that
    # have not registered (yet). Entries die with the connection, so "connected
    # client on view X" means exactly "an open, registered WebSocket whose latest
    # report named X".
    _client_info_by_queue_id: dict[int, dict[str, str]] = PrivateAttr(default_factory=dict)

    def register(self) -> queue.Queue[str | None]:
        """Register a new WebSocket client. Returns a queue to drain for messages."""
        client_queue: queue.Queue[str | None] = queue.Queue(maxsize=_CLIENT_QUEUE_MAX_SIZE)
        with self._lock:
            self._client_queues.append(client_queue)
            self._consecutive_queue_full_by_id[id(client_queue)] = 0
        return client_queue

    def unregister(self, client_queue: queue.Queue[str | None]) -> None:
        """Remove a WebSocket client's queue."""
        with self._lock:
            self._consecutive_queue_full_by_id.pop(id(client_queue), None)
            self._client_info_by_queue_id.pop(id(client_queue), None)
            try:
                self._client_queues.remove(client_queue)
            except ValueError:
                pass

    def set_client_info(
        self,
        client_queue: queue.Queue[str | None],
        client_id: str,
        active_view: str,
        device_kind: str,
    ) -> None:
        """Record (or update) the self-reported identity of one connected client."""
        with self._lock:
            if client_queue not in self._client_queues:
                return
            self._client_info_by_queue_id[id(client_queue)] = {
                "client_id": client_id,
                "active_view": active_view,
                "device_kind": device_kind,
            }

    def get_connected_client_infos(self) -> list[dict[str, str]]:
        """A snapshot of every registered client's self-reported identity."""
        with self._lock:
            return [dict(info) for info in self._client_info_by_queue_id.values()]

    def get_client_info(self, client_queue: queue.Queue[str | None]) -> dict[str, str] | None:
        """The self-reported identity of one connected client, or None if unregistered."""
        with self._lock:
            info = self._client_info_by_queue_id.get(id(client_queue))
            return dict(info) if info is not None else None

    def has_client_on_view(self, view_id: str) -> bool:
        """Whether any registered client currently has ``view_id`` active."""
        with self._lock:
            return any(info["active_view"] == view_id for info in self._client_info_by_queue_id.values())

    def broadcast(self, message: dict[str, Any]) -> None:
        """Serialize and send a message to all connected clients. Thread-safe."""
        self._broadcast_to_matching(message, target_view=None)

    def broadcast_to_view(self, message: dict[str, Any], view_id: str) -> None:
        """Send a message only to registered clients whose active view is ``view_id``.

        Clients that have not (yet) sent their ``client_state`` registration
        never match: without a report there is no view to compare against.
        """
        self._broadcast_to_matching(message, target_view=view_id)

    def _broadcast_to_matching(self, message: dict[str, Any], target_view: str | None) -> None:
        text = json.dumps(message)
        with self._lock:
            dead_queues: list[queue.Queue[str | None]] = []
            for client_queue in self._client_queues:
                if target_view is not None:
                    info = self._client_info_by_queue_id.get(id(client_queue))
                    if info is None or info["active_view"] != target_view:
                        continue
                try:
                    client_queue.put_nowait(text)
                    self._consecutive_queue_full_by_id[id(client_queue)] = 0
                except queue.Full:
                    new_count = self._consecutive_queue_full_by_id.get(id(client_queue), 0) + 1
                    self._consecutive_queue_full_by_id[id(client_queue)] = new_count
                    if new_count >= _MAX_CONSECUTIVE_QUEUE_FULL:
                        dead_queues.append(client_queue)
            for dead_queue in dead_queues:
                self._disconnect_locked(dead_queue)

    def _disconnect_locked(self, dead_queue: queue.Queue[str | None]) -> None:
        """Evict ``dead_queue`` and unblock its handler thread. Caller must hold ``self._lock``.

        Drains the queue and pushes the shutdown sentinel so the handler thread,
        blocked on ``client_queue.get(...)``, wakes, sees ``None``, and exits its
        loop (closing its socket). This is the thread-based replacement for the
        old asyncio task cancellation.
        """
        self._consecutive_queue_full_by_id.pop(id(dead_queue), None)
        self._client_info_by_queue_id.pop(id(dead_queue), None)
        try:
            self._client_queues.remove(dead_queue)
        except ValueError:
            pass
        _drain_queue(dead_queue)
        try:
            dead_queue.put_nowait(None)
        except queue.Full:
            pass
        _loguru_logger.warning(
            "Disconnected unresponsive WebSocket client after {} consecutive queue-full broadcasts",
            _MAX_CONSECUTIVE_QUEUE_FULL,
        )

    def broadcast_agents_updated(self, agents: list[dict[str, Any]]) -> None:
        """Broadcast an agents_updated event."""
        self.broadcast({"type": "agents_updated", "agents": agents})

    def broadcast_apps_updated(self, apps: Sequence[Mapping[str, Any]]) -> None:
        """Broadcast the whole inventory (contracts.md section 8): every app with its instances."""
        self.broadcast({"type": "apps_updated", "apps": apps})

    def broadcast_projects_updated(self, projects: Sequence[Mapping[str, Any]]) -> None:
        """Broadcast every project after a project write (contracts.md section 8)."""
        self.broadcast({"type": "projects_updated", "projects": projects})

    def broadcast_tab_rebound(self, client_id: str, view_id: str, tab_id: str, address: str) -> None:
        """Tell the owning client that one of its tabs now shows another instance (the tab route)."""
        self.broadcast(
            {"type": "tab_rebound", "client_id": client_id, "view_id": view_id, "tab_id": tab_id, "address": address}
        )

    def broadcast_proto_agent_created(
        self,
        agent_id: str,
        name: str,
        creation_type: str,
        parent_agent_id: str | None,
    ) -> None:
        """Broadcast a proto_agent_created event."""
        self.broadcast(
            {
                "type": "proto_agent_created",
                "agent_id": agent_id,
                "name": name,
                "creation_type": creation_type,
                "parent_agent_id": parent_agent_id,
            }
        )

    def broadcast_proto_agent_completed(self, agent_id: str, success: bool, error: str | None) -> None:
        """Broadcast a proto_agent_completed event."""
        self.broadcast(
            {
                "type": "proto_agent_completed",
                "agent_id": agent_id,
                "success": success,
                "error": error,
            }
        )

    def broadcast_layout_op(
        self,
        op: str,
        args: dict[str, Any],
        requester_agent_id: str = "",
        target_view: str | None = None,
    ) -> None:
        """Broadcast a layout_op event telling the frontend to mutate the dockview layout.

        The frontend dispatches on ``op`` (``open``, ``focus``, ``split``, ``move``, ``close``,
        ``maximize``, ``restore``, ``refresh``, ``reload_system_interface``) and applies the
        corresponding dockview primitive. ``args`` is an op-specific payload keyed by address.

        ``requester_agent_id`` is the ``MNGR_AGENT_ID`` of the agent that invoked
        ``system/scripts/layout.py``; the frontend anchors splits against the requester's
        own chat tab and resolves the ``self`` address with it.

        ``target_view`` restricts delivery to clients whose active view matches (mutating
        ops are view-targeted); None broadcasts to everyone (``refresh``,
        ``reload_system_interface``).
        """
        message = {"type": "layout_op", "op": op, "args": args, "requester_agent_id": requester_agent_id}
        if target_view is None:
            self.broadcast(message)
        else:
            self.broadcast_to_view(message, target_view)

    def broadcast_load_layout(self, view_id: str, display_name: str, target_client_id: str | None) -> None:
        """Broadcast an agent-driven request that a client switch to a view.

        ``target_client_id`` names the one client that should switch; None means every
        client switches (the fallback when the requesting client could not be resolved).
        CLEANUP: phase 8 of the workspace app model folds this into ``active_view_changed``.
        """
        self.broadcast(
            {
                "type": "load_layout",
                "view_id": view_id,
                "display_name": display_name,
                "target_client_id": target_client_id,
            }
        )

    def shutdown(self) -> None:
        """Signal all clients to disconnect by sending None sentinel."""
        with self._lock:
            for client_queue in self._client_queues:
                _drain_queue(client_queue)
                try:
                    client_queue.put_nowait(None)
                except queue.Full:
                    pass
            self._client_queues.clear()
            self._consecutive_queue_full_by_id.clear()
            self._client_info_by_queue_id.clear()
