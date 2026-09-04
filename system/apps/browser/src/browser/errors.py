from app_instances.errors import AppInstancesError


class BrowserFleetError(AppInstancesError):
    """Base error for the fleet as the instances adapter drives it; a library error, so the instances blueprint answers one the adapter does not map with a detail body."""


class InvalidBrowserNameValueError(BrowserFleetError, ValueError):
    """A string is not a browser name (see ``names.is_valid_browser_name``)."""


class FleetCreateRefusedError(BrowserFleetError):
    """The fleet cannot take a new browser right now: it is full, or Chromium is not installed yet."""


class UnknownBrowserError(BrowserFleetError):
    """No registered browser has the given name."""


class BrowserNotDrivableError(BrowserFleetError):
    """The browser cannot be navigated: Chromium is still launching, or it crashed."""


class BrowserHeldByAgentError(BrowserFleetError):
    """An agent holds the browser, and agents are never preempted."""


class NavigationFailedError(BrowserFleetError):
    """Chromium refused or never finished a navigation."""


class FleetUnavailableError(BrowserFleetError):
    """The daemon could not complete a verb: its loop did not answer in time, or a startup failure or a lost Chromium underneath it."""
