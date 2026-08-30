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
    """Thread-safe registry of per-agent SSE delivery queues.

    Delivery is live-only: nothing is buffered for replay, because every event is
    recoverable over the REST ``/events`` endpoint -- the stream is a low-latency hint and
    the REST snapshot is the source of truth. (An earlier replay buffer inherited from
    llm-webchat's ConversationEventQueues grew per-agent forever and duplicated the REST
    path; it is gone.)

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

    def register(self, agent_id: str) -> queue.Queue[dict[str, Any] | None]:
        event_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=_MAX_QUEUED_EVENTS)
        with self._lock:
            if self._shutdown:
                event_queue.put_nowait(None)
                return event_queue
            self._queues[agent_id].append(event_queue)
        return event_queue

    def unregister(self, agent_id: str, event_queue: queue.Queue[dict[str, Any] | None]) -> None:
        with self._lock:
            queues = self._queues.get(agent_id)
            if queues is not None:
                try:
                    queues.remove(event_queue)
                except ValueError:
                    pass
                if not queues:
                    del self._queues[agent_id]

    def broadcast(self, agent_id: str, event: dict[str, Any]) -> None:
        """Deliver one event to every live consumer for ``agent_id`` (the plugin-hook shape)."""
        self.broadcast_batch(agent_id, [event])

    def broadcast_batch(self, agent_id: str, events: list[dict[str, Any]]) -> None:
        """Deliver a batch of events, evicting any consumer whose queue overflows."""
        with self._lock:
            queues = list(self._queues.get(agent_id, []))
            for event_queue in queues:
                for event in events:
                    try:
                        event_queue.put_nowait(event)
                    except queue.Full:
                        self._evict_locked(agent_id, event_queue)
                        break

    def _evict_locked(self, agent_id: str, event_queue: queue.Queue[dict[str, Any] | None]) -> None:
        """Disconnect one overflowing consumer. Caller must hold ``self._lock``.

        Drains the queue and pushes the shutdown sentinel so the handler thread, blocked on
        ``get``, wakes, sees ``None``, and closes its stream -- which triggers the client's
        reconnect-with-snapshot resync.
        """
        self.unregister(agent_id, event_queue)
        _drain_queue(event_queue)
        try:
            event_queue.put_nowait(None)
        except queue.Full:
            pass
        logger.warning("Disconnected an SSE consumer for agent {}: its event queue overflowed", agent_id)

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            for agent_queues in self._queues.values():
                for event_queue in agent_queues:
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
