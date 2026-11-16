"""The service's own events to viewers (docs/system/specs/screen/spec.md section 10.2): one outbound queue per
``/api/ws`` client, drained by the thread that owns its socket."""

import json
import queue
import threading
from typing import Any
from typing import Final

from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel

# A viewer that cannot keep up gets the newest events: the oldest buffered one is dropped to make room.
QUEUE_SIZE: Final[int] = 256


class ViewerBroadcaster(MutableModel):
    """Fans a JSON event out to every connected viewer."""

    model_config = {"arbitrary_types_allowed": True}

    queue_size: int = Field(default=QUEUE_SIZE, frozen=True, description="How many events a slow viewer may lag")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _queues: list["queue.Queue[str | None]"] = PrivateAttr(default_factory=list)

    def register(self) -> "queue.Queue[str | None]":
        client_queue: queue.Queue[str | None] = queue.Queue(maxsize=self.queue_size)
        with self._lock:
            self._queues.append(client_queue)
        return client_queue

    def unregister(self, client_queue: "queue.Queue[str | None]") -> None:
        with self._lock:
            if client_queue in self._queues:
                self._queues.remove(client_queue)

    def connected_count(self) -> int:
        with self._lock:
            return len(self._queues)

    def broadcast(self, event: dict[str, Any]) -> None:
        text = json.dumps(event, default=str)
        with self._lock:
            queues = list(self._queues)
        for client_queue in queues:
            try:
                client_queue.put_nowait(text)
            except queue.Full:
                try:
                    client_queue.get_nowait()
                    client_queue.put_nowait(text)
                except (queue.Empty, queue.Full):
                    pass

    def shutdown(self) -> None:
        with self._lock:
            queues = list(self._queues)
        for client_queue in queues:
            try:
                client_queue.put_nowait(None)
            except queue.Full:
                pass
