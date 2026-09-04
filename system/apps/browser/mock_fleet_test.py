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
    navigations: list[tuple[BrowserName, str]] = Field(
        default_factory=list, description="Every (name, url) navigated"
    )

    def is_ready(self) -> bool:
        return self.is_fleet_ready

    def list_browsers(self) -> list[BrowserSnapshot]:
        return list(self.browsers)

    def create_browser(self) -> BrowserSnapshot:
        if self.create_refusal is not None:
            raise FleetCreateRefusedError(self.create_refusal)
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

    def navigate_browser(self, name: BrowserName, url: str) -> None:
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
