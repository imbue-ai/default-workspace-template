import threading

from app_instances.interfaces import InstanceNudgerInterface
from app_instances.primitives import AbsoluteHttpUrl
from pydantic import Field

from browser.data_types import BrowserSnapshot
from browser.errors import FleetCreateRefusedError, UnknownBrowserError
from browser.interfaces import FleetInterface
from browser.loop_bridge import AsyncLoopBridge
from browser.primitives import BrowserName
from browser.session import (
    BrowserSessionManager,
    FleetFullError,
    deferred_install_ready,
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
        return self.bridge.run(
            self.manager.snapshot_browsers(), timeout=self.route_timeout_seconds
        )

    def create_browser(self) -> BrowserSnapshot:
        is_installed, reason = deferred_install_ready()
        if not is_installed:
            raise FleetCreateRefusedError(reason)
        try:
            return self.bridge.run(
                self.manager.create_snapshot(), timeout=self.route_timeout_seconds
            )
        except FleetFullError as e:
            raise FleetCreateRefusedError(str(e)) from e

    def close_browser(self, name: BrowserName) -> None:
        self.bridge.run(
            self.manager.close_and_forget(name), timeout=self.route_timeout_seconds
        )

    def navigate_browser(self, name: BrowserName, url: AbsoluteHttpUrl) -> None:
        try:
            self.bridge.run(
                self.manager.navigate_browser(name, url),
                timeout=self.route_timeout_seconds,
            )
        except KeyError as e:
            raise UnknownBrowserError(f"no browser named {name!r}") from e


class ManagerNudger(InstanceNudgerInterface):
    """Nudges through whatever nudger the manager currently has, so the blueprint's route nudges and the fleet's own share one installation point."""

    manager: BrowserSessionManager = Field(frozen=True, description="The fleet")

    def nudge(self) -> None:
        self.manager.nudge()
