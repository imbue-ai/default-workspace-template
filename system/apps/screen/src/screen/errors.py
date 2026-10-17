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


class ServeError(ScreenError):
    """The page server could not bind or shut down as asked."""
