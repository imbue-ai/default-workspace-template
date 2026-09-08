from app_instances.primitives import AbsoluteHttpUrl
from browser.data_types import BrowserController, BrowserLifecycle, BrowserSnapshot
from browser.errors import (
    BrowserHeldByAgentError,
    BrowserNotDrivableError,
    FleetCreateRefusedError,
    NavigationFailedError,
    UnknownBrowserError,
)
from browser.interfaces import FleetInterface
from browser.names import first_free_numbered_browser_name
from browser.primitives import BrowserName
from pydantic import Field


class FakeFleet(FleetInterface):
    """An in-memory fleet with the same refusals as the real one, recording every verb."""

    browsers: list[BrowserSnapshot] = Field(
        default_factory=list, description="The registered browsers, in order"
    )
    is_fleet_ready: bool = Field(
        default=True, description="Whether the init gate is open"
    )
    create_refusal: str | None = Field(
        default=None,
        description="When set, create raises FleetCreateRefusedError with this detail",
    )
    navigation_failure: str | None = Field(
        default=None,
        description="When set, every navigation raises NavigationFailedError with this detail",
    )
    closed_names: list[BrowserName] = Field(
        default_factory=list, description="Every name close was asked for"
    )
    navigations: list[tuple[BrowserName, AbsoluteHttpUrl]] = Field(
        default_factory=list, description="Every (name, url) navigated"
    )
    start_urls: list[AbsoluteHttpUrl | None] = Field(
        default_factory=list,
        description="The start page each create asked for, in order",
    )

    def is_ready(self) -> bool:
        return self.is_fleet_ready

    def list_browsers(self) -> list[BrowserSnapshot]:
        return list(self.browsers)

    def create_browser(self, start_url: AbsoluteHttpUrl | None) -> BrowserSnapshot:
        if self.create_refusal is not None:
            raise FleetCreateRefusedError(self.create_refusal)
        self.start_urls.append(start_url)
        name = first_free_numbered_browser_name(
            {snapshot.name for snapshot in self.browsers}
        )
        snapshot = BrowserSnapshot(
            name=BrowserName(name),
            lifecycle=BrowserLifecycle.INIT,
            controller=BrowserController.HUMAN,
        )
        self.browsers.append(snapshot)
        return snapshot

    def close_browser(self, name: BrowserName) -> None:
        self.closed_names.append(name)
        self.browsers = [
            snapshot for snapshot in self.browsers if snapshot.name != name
        ]

    def stop_browser(self, name: BrowserName) -> None:
        snapshot = self._find(name)
        if snapshot.lifecycle == BrowserLifecycle.INIT:
            raise BrowserNotDrivableError(f"browser {name} is still launching")
        self._set_lifecycle(name, BrowserLifecycle.STOPPED)

    def start_browser(self, name: BrowserName) -> None:
        snapshot = self._find(name)
        if snapshot.lifecycle in (BrowserLifecycle.INIT, BrowserLifecycle.RUNNING):
            return
        if self.create_refusal is not None:
            raise FleetCreateRefusedError(self.create_refusal)
        self._set_lifecycle(name, BrowserLifecycle.INIT)

    def _find(self, name: BrowserName) -> BrowserSnapshot:
        snapshot = next(
            (snapshot for snapshot in self.browsers if snapshot.name == name), None
        )
        if snapshot is None:
            raise UnknownBrowserError(f"no browser named {name!r}")
        return snapshot

    def _set_lifecycle(self, name: BrowserName, lifecycle: BrowserLifecycle) -> None:
        self.browsers = [
            BrowserSnapshot(name=snapshot.name, lifecycle=lifecycle, controller=BrowserController.HUMAN)
            if snapshot.name == name
            else snapshot
            for snapshot in self.browsers
        ]

    def navigate_browser(self, name: BrowserName, url: AbsoluteHttpUrl) -> None:
        snapshot = next(
            (snapshot for snapshot in self.browsers if snapshot.name == name), None
        )
        if snapshot is None:
            raise UnknownBrowserError(f"no browser named {name!r}")
        if snapshot.lifecycle != BrowserLifecycle.RUNNING:
            raise BrowserNotDrivableError(f"browser {name} is {snapshot.lifecycle}")
        if snapshot.controller == BrowserController.AGENT:
            raise BrowserHeldByAgentError(f"browser {name} is held by an agent")
        if self.navigation_failure is not None:
            raise NavigationFailedError(self.navigation_failure)
        self.navigations.append((name, url))
