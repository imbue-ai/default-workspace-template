"""Fan-out of newly stored model_call events to the clients streaming ``GET /v1/stream``.

Each subscriber gets a bounded queue. A subscriber that stops reading is
dropped once its queue fills, rather than holding events in memory for a
client that has gone.
"""

import queue
import threading
from typing import Final

from imbue.mngr_usage.model_call import ModelCallEvent

_SUBSCRIBER_QUEUE_SIZE: Final[int] = 1000


class Subscription:
    """One stream client's queue of serialized events; ``is_open`` turns False once it is dropped."""

    def __init__(self) -> None:
        self.events: queue.Queue[str] = queue.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        self.is_open = True


class UsageFeed:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscriptions: set[Subscription] = set()

    def subscribe(self) -> Subscription:
        subscription = Subscription()
        with self._lock:
            self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        subscription.is_open = False
        with self._lock:
            self._subscriptions.discard(subscription)

    def close(self) -> None:
        with self._lock:
            subscriptions = list(self._subscriptions)
        for subscription in subscriptions:
            self.unsubscribe(subscription)

    def publish(self, event: ModelCallEvent) -> None:
        message = event.model_dump_json()
        with self._lock:
            subscriptions = list(self._subscriptions)
        for subscription in subscriptions:
            try:
                subscription.events.put_nowait(message)
            except queue.Full:
                self.unsubscribe(subscription)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)
