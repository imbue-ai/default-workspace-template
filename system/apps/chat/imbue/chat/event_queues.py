import queue
import threading
from collections import defaultdict
from typing import Any
from typing import Final

from loguru import logger as _loguru_logger

logger = _loguru_logger

# Per-connection queue depth. Transcript deltas are bursty but small; a healthy SSE
# generator drains continuously, so a full queue means the consumer has stopped draining
# entirely (a wedged socket write), not that it is momentarily behind.
_MAX_QUEUED_EVENTS: Final[int] = 1000


class AgentEventQueues:
    """Thread-safe registry of per-chat SSE delivery queues.

    Keyed by chat id rather than agent id, so a page's stream follows its chat through a
    handoff: the successor's events and the switch chip arrive on the connection the
    retiring agent's events did.

    Delivery is live-only: nothing is buffered for replay, because every event is
    recoverable over the REST ``/events`` endpoint -- the stream is a low-latency hint and
    the REST snapshot is the source of truth.

    Each connection's queue is bounded, and a consumer whose queue overflows is evicted on
    the FIRST full ``put``: unlike the agents WebSocket's snapshot traffic (where the next
    snapshot supersedes a dropped one), one dropped transcript delta silently desyncs the
    stream, so the only honest response is closing it -- drain the queue and push the
    ``None`` sentinel so the handler thread exits and the client's reconnect-with-snapshot
    resyncs it.
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[queue.Queue[dict[str, Any] | None]]] = defaultdict(list)
        # Reentrant for two same-thread re-entries into unregister() while the
        # lock is held. Deliberate: broadcast_batch evicts an overflowing
        # consumer from inside its locked delivery loop (_evict_locked ->
        # unregister). Indirect: a CPython GC cycle during an allocation inside
        # a locked section can finalize an abandoned SSE event_generator (from
        # an unrelated prior stream), whose `finally` block calls unregister()
        # on the same thread. With a non-reentrant Lock either re-entrance
        # self-deadlocks.
        self._lock: threading.RLock = threading.RLock()
        self._shutdown: bool = False

    @property
    def is_shutdown(self) -> bool:
        return self._shutdown

    def register(self, chat_id: str) -> queue.Queue[dict[str, Any] | None]:
        event_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=_MAX_QUEUED_EVENTS)
        with self._lock:
            if self._shutdown:
                event_queue.put_nowait(None)
                return event_queue
            self._queues[chat_id].append(event_queue)
        return event_queue

    def unregister(self, chat_id: str, event_queue: queue.Queue[dict[str, Any] | None]) -> None:
        with self._lock:
            queues = self._queues.get(chat_id)
            if queues is not None:
                try:
                    queues.remove(event_queue)
                except ValueError:
                    pass
                if not queues:
                    del self._queues[chat_id]

    def broadcast(self, chat_id: str, event: dict[str, Any]) -> None:
        """Deliver one event to every live consumer for ``chat_id`` (the plugin-hook shape)."""
        self.broadcast_batch(chat_id, [event])

    def broadcast_batch(self, chat_id: str, events: list[dict[str, Any]]) -> None:
        """Deliver a batch of events to the chat's consumers, evicting any whose queue overflows."""
        with self._lock:
            queues = list(self._queues.get(chat_id, []))
            for event_queue in queues:
                for event in events:
                    try:
                        event_queue.put_nowait(event)
                    except queue.Full:
                        self._evict_locked(chat_id, event_queue)
                        break

    def _evict_locked(self, chat_id: str, event_queue: queue.Queue[dict[str, Any] | None]) -> None:
        """Disconnect one overflowing consumer. Caller must hold ``self._lock``.

        Drains the queue and pushes the shutdown sentinel so the handler thread, blocked on
        ``get``, wakes, sees ``None``, and closes its stream -- which triggers the client's
        reconnect-with-snapshot resync.
        """
        self.unregister(chat_id, event_queue)
        _drain_queue(event_queue)
        try:
            event_queue.put_nowait(None)
        except queue.Full:
            pass
        logger.warning("Disconnected an SSE consumer for chat {}: its event queue overflowed", chat_id)

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            for chat_queues in self._queues.values():
                for event_queue in chat_queues:
                    _drain_queue(event_queue)
                    try:
                        event_queue.put_nowait(None)
                    except queue.Full:
                        pass
            self._queues.clear()


def _drain_queue(event_queue: queue.Queue[dict[str, Any] | None]) -> None:
    is_drained = False
    while not is_drained:
        try:
            event_queue.get_nowait()
        except queue.Empty:
            is_drained = True
