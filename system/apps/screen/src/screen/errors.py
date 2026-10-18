class ScreenError(Exception):
    """Base error for the Screen service and its command."""


class ScreenConfigurationError(ScreenError, ValueError):
    """A rendered configuration or a setting the service reads is not usable."""


class SupervisorError(ScreenError):
    """supervisord could not be asked to act on a program, or refused."""


class StackStartError(ScreenError):
    """The stack did not come up: a member failed to start, or neko never answered its health route."""


class NekoError(ScreenError):
    """neko refused a request or could not be reached."""


class NekoAuthenticationError(NekoError):
    """neko refused a login, or a session the service holds is gone."""


class ScreenNotRunningError(ScreenError):
    """An operation needs the stack running (a screenshot, a window list) and it is not."""


class InvalidIdentityError(ScreenError, ValueError):
    """The identity header a request carries cannot be read."""


class InvalidRunCommandError(ScreenError, ValueError):
    """A command to run on the Screen is empty or not a list of strings."""


class WindowToolError(ScreenError):
    """wmctrl or xdotool failed or answered something unreadable."""


class DisplayModeError(ScreenError):
    """xrandr failed, or the display advertises no output to add a mode to."""


class ClipboardError(ScreenError):
    """xclip could not set or read the X clipboard."""


class InvalidScreenSizeError(ScreenError, ValueError):
    """A requested size is not a pair of positive integers."""


class InvalidDroppedFileNameError(ScreenError, ValueError):
    """A dropped file's name is not one plain path segment (see ``primitives.describe_dropped_file_name_problem``)."""


class FileDropError(ScreenError):
    """A dropped file could not be written into the desktop folder."""


class ServeError(ScreenError):
    """The page server could not bind or shut down as asked."""


class ConnectivityBrokerError(ScreenError):
    """A connectivity broker (the desktop through the latchkey proxy, the share gateway) refused or could not be reached."""


class InvalidBrowserNameError(ScreenError, ValueError):
    """A string is not a browser name (see ``browser_names.is_valid_browser_name``)."""


class InvalidStartUrlError(ScreenError, ValueError):
    """A start page is not an absolute http(s) URL with a host, or not one on this machine where that is required."""


class UnknownBrowserError(ScreenError):
    """No registered browser has the given name."""


class DuplicateBrowserNameError(ScreenError):
    """A chosen browser name collides with a live browser, a saved one, or an on-disk profile."""


class FleetFullError(ScreenError):
    """The fleet already holds as many launched browsers as it may."""


class BrowserNotDrivableError(ScreenError):
    """The browser cannot be driven right now: Chromium is still launching, it crashed, it is stopped, or the
    fleet has no connection to it."""


class ChromiumNotInstalledError(ScreenError):
    """Fortress has not been installed by the env.d unit yet."""


class ChromiumStartError(ScreenError):
    """Chromium did not come up, or never published a debug port."""


class CdpError(ScreenError):
    """A CDP call returned an error payload, timed out, or could not be delivered."""
