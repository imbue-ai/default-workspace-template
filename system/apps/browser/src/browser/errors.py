class BrowserFleetError(Exception):
    """Base error for the fleet's verbs: what the daemon's routes and the fleet CLI answer with a status code."""


class InvalidBrowserNameValueError(BrowserFleetError, ValueError):
    """A string is not a browser name (see ``names.is_valid_browser_name``)."""


class InvalidStartUrlError(BrowserFleetError, ValueError):
    """A start page is not an absolute http(s) URL with a host."""


class UnknownBrowserError(BrowserFleetError):
    """No registered browser has the given name."""


class BrowserNotDrivableError(BrowserFleetError):
    """The browser cannot be driven right now (stopped, started, or its tab closed): Chromium is still launching, it crashed, it is not running, or the fleet has no connection to it."""
