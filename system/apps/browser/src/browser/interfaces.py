from abc import ABC, abstractmethod

from imbue.imbue_common.mutable_model import MutableModel

from browser.data_types import BrowserSnapshot
from browser.primitives import BrowserName


class FleetInterface(MutableModel, ABC):
    """The browser fleet as the instances adapter drives it: named browsers with an ownership state, reached from any thread."""

    @abstractmethod
    def is_ready(self) -> bool:
        """Whether the daemon has finished restoring the saved fleet (its init gate is open)."""

    @abstractmethod
    def list_browsers(self) -> list[BrowserSnapshot]:
        """Every registered browser, by name."""

    @abstractmethod
    def create_browser(self) -> BrowserSnapshot:
        """Register a new daemon-named browser and start its launch; raises FleetCreateRefusedError when the fleet cannot take one now."""

    @abstractmethod
    def close_browser(self, name: BrowserName) -> None:
        """Close the browser, retire its name, and delete its profile; an unknown name is not an error."""

    @abstractmethod
    def navigate_browser(self, name: BrowserName, url: str) -> None:
        """Navigate the browser's active tab; raises UnknownBrowserError, BrowserNotDrivableError, BrowserHeldByAgentError, or NavigationFailedError."""
