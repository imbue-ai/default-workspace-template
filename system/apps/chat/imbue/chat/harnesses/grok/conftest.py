from collections.abc import Iterator

import pytest

from imbue.chat.harnesses.grok import queue_tracker


@pytest.fixture(autouse=True)
def _empty_grok_tracker_registry() -> Iterator[None]:
    """Give every grok test an empty tracker registry, and leave none of its trackers behind."""
    with queue_tracker._TRACKERS_LOCK:
        queue_tracker._TRACKERS.clear()
    yield
    with queue_tracker._TRACKERS_LOCK:
        queue_tracker._TRACKERS.clear()
