import json
import queue
from typing import Any

from screen.broadcast import ViewerBroadcaster


def _next_event(client_queue: "queue.Queue[str | None]") -> dict[str, Any]:
    message = client_queue.get_nowait()
    assert message is not None
    return json.loads(message)


def test_broadcast_reaches_every_registered_viewer_and_drops_the_oldest_for_a_slow_one() -> None:
    broadcaster = ViewerBroadcaster(queue_size=2)
    first = broadcaster.register()
    second = broadcaster.register()
    for index in range(3):
        broadcaster.broadcast({"type": "state", "index": index})
    assert broadcaster.connected_count() == 2
    assert [_next_event(first)["index"] for _ in range(2)] == [1, 2]
    assert [_next_event(second)["index"] for _ in range(2)] == [1, 2]
    broadcaster.unregister(first)
    broadcaster.broadcast({"type": "state", "index": 3})
    assert first.empty()
    assert _next_event(second)["index"] == 3
    broadcaster.shutdown()
    assert second.get_nowait() is None
