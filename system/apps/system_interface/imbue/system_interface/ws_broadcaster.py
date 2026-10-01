import json
import queue
import threading
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
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


class ConnectionRegistration(FrozenModel):
    """What one open WebSocket said about itself in its ``client_state`` report (desktop contracts.md section 6)."""

    client_id: str = Field(description="The client the connection's window belongs to")
    active_desktop: str = Field(
        description="The desktop the window last reported being on; empty for a pop-out's, which never reports one"
    )
    is_pop_out: bool = Field(
        description="Whether the window is a pulled-out window's own desktop window rather than a desktop's"
    )


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
    # Self-reported identity of each connected window, keyed by ``id(queue)``. Populated when the window
    # sends its ``client_state`` registration over the WebSocket; absent for windows that have not registered
    # (yet). Entries die with the connection, so "connected client on desktop X" means exactly "an open,
    # registered WebSocket whose latest report named X". A pop-out's registration names its client (so ops
    # targeting the client reach it) and no desktop.
    _client_info_by_queue_id: dict[int, ConnectionRegistration] = PrivateAttr(default_factory=dict)

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

    def set_client_info(self, client_queue: queue.Queue[str | None], client_id: str, active_desktop: str) -> None:
        """Record (or update) the self-reported identity of one connected desktop window: its client and the
        desktop it is on."""
        self._register(
            client_queue, ConnectionRegistration(client_id=client_id, active_desktop=active_desktop, is_pop_out=False)
        )

    def set_pop_out_info(self, client_queue: queue.Queue[str | None], client_id: str) -> None:
        """Record one connected pop-out window under its client: ops targeting the client reach it, and it counts
        toward the client being connected, but it names no desktop."""
        self._register(client_queue, ConnectionRegistration(client_id=client_id, active_desktop="", is_pop_out=True))

    def _register(self, client_queue: queue.Queue[str | None], registration: ConnectionRegistration) -> None:
        with self._lock:
            if client_queue not in self._client_queues:
                return
            self._client_info_by_queue_id[id(client_queue)] = registration

    def get_connected_client_infos(self) -> list[ConnectionRegistration]:
        """A snapshot of every registered window's self-reported identity."""
        with self._lock:
            return list(self._client_info_by_queue_id.values())

    def get_client_info(self, client_queue: queue.Queue[str | None]) -> ConnectionRegistration | None:
        """The self-reported identity of one connected window, or None if unregistered."""
        with self._lock:
            return self._client_info_by_queue_id.get(id(client_queue))

    def connected_client_ids(self) -> set[str]:
        """The ids of every registered client with at least one open window, a pop-out included."""
        with self._lock:
            return {info.client_id for info in self._client_info_by_queue_id.values()}

    def desktop_connected_client_ids(self) -> set[str]:
        """The ids of every registered client with a desktop window open: a client whose only open windows are
        pop-outs is connected, but has nowhere to show a desktop window."""
        with self._lock:
            return {info.client_id for info in self._client_info_by_queue_id.values() if not info.is_pop_out}

    def broadcast(self, message: dict[str, Any]) -> None:
        """Serialize and send a message to all connected clients. Thread-safe."""
        self._broadcast_to_matching(message, target_client_id=None)

    def broadcast_to_client(self, message: dict[str, Any], client_id: str) -> None:
        """Send a message only to the windows of one client (every registered connection carrying its id).

        Connections that have not (yet) sent their ``client_state`` registration never match:
        without a report there is no client id to compare against.
        """
        self._broadcast_to_matching(message, target_client_id=client_id)

    def _broadcast_to_matching(self, message: dict[str, Any], target_client_id: str | None) -> None:
        text = json.dumps(message)
        with self._lock:
            dead_queues: list[queue.Queue[str | None]] = []
            for client_queue in self._client_queues:
                if target_client_id is not None:
                    info = self._client_info_by_queue_id.get(id(client_queue))
                    if info is None or info.client_id != target_client_id:
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
        loop (closing its socket).
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

    def broadcast_apps_updated(self, apps: Sequence[Mapping[str, Any]]) -> None:
        """Broadcast every app after a registry or liveness change (desktop contracts.md section 6)."""
        self.broadcast({"type": "apps_updated", "apps": apps})

    def broadcast_desktops_updated(self, desktops: Sequence[Mapping[str, Any]]) -> None:
        """Broadcast every desktop after a write of ``desktops.json`` (desktop contracts.md section 6)."""
        self.broadcast({"type": "desktops_updated", "desktops": desktops})

    def broadcast_presence_updated(self, users: Sequence[Mapping[str, Any]]) -> None:
        """Broadcast the connected users whenever someone joins or leaves (one entry per user)."""
        self.broadcast({"type": "presence_updated", "users": users})

    def broadcast_placements_updated(self, desktop_id: str, client_id: str, save_id: str) -> None:
        """A client's layout of a desktop was written (a browser's save or the shell's own edit); the owning windows refetch."""
        self.broadcast(
            {
                "type": "placements_updated",
                "desktop_id": desktop_id,
                "client_id": client_id,
                "save_id": save_id,
            }
        )

    def broadcast_avatar_status(self, status: Mapping[str, Any]) -> None:
        """The avatar's mood or staleness changed (pinned-taskbar-entries plan section 4.6); every window redraws it."""
        self.broadcast({"type": "avatar_status", **status})

    def broadcast_avatar_selection_changed(self, design: str) -> None:
        """The workspace's avatar design was written; every window draws it."""
        self.broadcast({"type": "avatar_selection_changed", "design": design})

    def broadcast_client_entries_changed(self, client_id: str, entries: Mapping[str, Any]) -> None:
        """A client's presentation of its pinned entries was written; its own windows take it."""
        self.broadcast_to_client(
            {"type": "client_entries_changed", "client_id": client_id, "entries": entries}, client_id
        )

    def broadcast_active_desktop_changed(self, client_id: str, desktop_id: str) -> None:
        """A client's stored active desktop moved; its other windows switch to it."""
        self.broadcast({"type": "active_desktop_changed", "client_id": client_id, "desktop_id": desktop_id})

    def broadcast_update_notice_changed(self, notice: Mapping[str, Any] | None) -> None:
        """The kept rollback point changed (raised, progressing, settled, or cleared); every window re-renders its notice."""
        self.broadcast({"type": "update_notice_changed", "notice": dict(notice) if notice is not None else None})

    def broadcast_layout_op(
        self,
        op: str,
        args: dict[str, Any],
        requester: str = "",
        target_client_id: str | None = None,
    ) -> None:
        """Send a transient ``layout_op`` to the browser (desktop contracts.md section 8): ``refresh`` and the
        interface reload, which are the whole effect of their ops, and the ``show``, ``open`` (unless minimized),
        and ``focus`` of a targeted op, which name the window the op put in front of the client after its edit was
        written, for the phone layout to switch to; a ``show`` also says whether the window is pulled out, since only
        the client's pages (the desktop's, and that window's own solo page) can bring a pulled-out window's own
        desktop window forward, and a ``focus`` on a window popped out travels as such a ``show``.

        ``requester`` is the app and marker of the chat that invoked ``workspace-layout``, spelled
        ``<app>:<marker>``. ``target_client_id`` names the client whose windows apply the op; None reaches
        every window (``refresh`` of a whole app, ``reload_system_interface``).
        """
        message = {
            "type": "layout_op",
            "op": op,
            "args": args,
            "requester": requester,
            "target_client_id": target_client_id,
        }
        if target_client_id is None:
            self.broadcast(message)
        else:
            self.broadcast_to_client(message, target_client_id)

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
