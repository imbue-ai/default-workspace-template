"""Shared helpers for the Screen's tests."""

import threading
import time
from collections.abc import Callable
from typing import Final

from screen.data_types import Lifecycle
from screen.service import ScreenService

_DEFAULT_WAIT_SECONDS: Final[float] = 10.0
_POLL_SECONDS: Final[float] = 0.02


def wait_until(predicate: Callable[[], bool], timeout_seconds: float = _DEFAULT_WAIT_SECONDS) -> bool:
    """Poll ``predicate`` until it holds or the deadline passes."""
    deadline = time.monotonic() + timeout_seconds
    pause = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return True
        pause.wait(_POLL_SECONDS)
    return predicate()


def start_service_stack(service: ScreenService) -> None:
    """Start the stack through its on-demand path and wait until it runs."""
    service.ensure_started()
    assert wait_until(lambda: service.stack.lifecycle == Lifecycle.RUNNING), service.stack.stop_reason
