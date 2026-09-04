import threading
from collections.abc import Coroutine
from typing import Any, Final, TypeVar

from app_instances.interfaces import InstanceNudgerInterface
from app_instances.primitives import AbsoluteHttpUrl
from pydantic import Field

from browser.data_types import BrowserSnapshot
from browser.errors import FleetCreateRefusedError, FleetUnavailableError
from browser.interfaces import FleetInterface
from browser.loop_bridge import AsyncLoopBridge
from browser.primitives import BrowserName
from browser.session import (
    BrowserSessionManager,
    BrowserStartupError,
    FleetFullError,
    deferred_install_ready,
)

_Result = TypeVar("_Result")

# What a verb run on the loop raises when the daemon itself fails underneath it: the loop
# did not answer within the route timeout, or the startup and Chromium-connection errors
# the daemon's own routes answer 503 with (``runner._STARTUP_ERRORS``).
_DAEMON_FAILURES: Final[tuple[type[BaseException], ...]] = (
    TimeoutError,
    BrowserStartupError,
    RuntimeError,
    ConnectionError,
    OSError,
)


class BridgedFleet(FleetInterface):
    """The real fleet, reached from Flask threads the way every daemon route reaches it: one coroutine per verb, run on the loop through the bridge."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    bridge: AsyncLoopBridge = Field(
        frozen=True, description="The daemon's one sync-to-async boundary"
    )
    manager: BrowserSessionManager = Field(frozen=True, description="The fleet")
    ready_gate: threading.Event = Field(
        frozen=True,
        description="The daemon's init gate: set once the saved fleet has been restored",
    )
    route_timeout_seconds: float = Field(
        frozen=True, description="How long one verb may wait on the loop"
    )

    def is_ready(self) -> bool:
        return self.ready_gate.is_set()

    def list_browsers(self) -> list[BrowserSnapshot]:
        return self._run_on_loop(self.manager.snapshot_browsers())

    def create_browser(self) -> BrowserSnapshot:
        is_installed, reason = deferred_install_ready()
        if not is_installed:
            raise FleetCreateRefusedError(reason)
        try:
            return self._run_on_loop(self.manager.create_snapshot())
        except FleetFullError as e:
            raise FleetCreateRefusedError(str(e)) from e

    def close_browser(self, name: BrowserName) -> None:
        self._run_on_loop(self.manager.close_and_forget(name))

    def navigate_browser(self, name: BrowserName, url: AbsoluteHttpUrl) -> None:
        self._run_on_loop(self.manager.navigate_browser(name, url))

    def _run_on_loop(self, coroutine: Coroutine[Any, Any, _Result]) -> _Result:
        """Run one verb on the loop, bounded by the route timeout.

        The fleet's own refusals come back as they are; a failure of the daemon underneath
        the verb is a FleetUnavailableError, so the instances blueprint answers it with a
        detail body rather than Flask's bare 500.
        """
        try:
            return self.bridge.run(coroutine, timeout=self.route_timeout_seconds)
        except FleetFullError:
            raise
        except _DAEMON_FAILURES as e:
            raise FleetUnavailableError(
                f"the browser daemon could not complete the request: {e}"
            ) from e


class ManagerNudger(InstanceNudgerInterface):
    """Nudges through whatever nudger the manager currently has, so the blueprint's route nudges and the fleet's own share one installation point."""

    manager: BrowserSessionManager = Field(frozen=True, description="The fleet")

    def nudge(self) -> None:
        self.manager.nudge()
