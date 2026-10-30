from enum import auto
from typing import Final

from imbue.imbue_common.enums import LowerCaseStrEnum


class RefusalKind(LowerCaseStrEnum):
    """What a refusal is, for a frontend that words it with what only it holds: the name of an app the shell no
    longer has, the label of a launch path an app no longer offers, the sentence of the press that failed (a wire
    value: the ``kind`` of an error answer)."""

    APP_UNAVAILABLE = auto()
    LAUNCH_PATH_GONE = auto()
    WINDOW_GONE = auto()
    # A state file could not be written: the frontend offers to try the change again.
    OUT_OF_SPACE = auto()
    NOT_SAVED = auto()


APP_UNAVAILABLE_SENTENCE: Final[str] = "This app isn't available in this workspace right now, so nothing was changed."
WINDOW_GONE_SENTENCE: Final[str] = "That window is no longer here, so nothing was changed. Try again."
DESKTOP_GONE_SENTENCE: Final[str] = "That desktop was just deleted, so nothing was changed."
LAST_DESKTOP_SENTENCE: Final[str] = "You can't delete your only desktop. Add another one first."
OUT_OF_SPACE_SENTENCE: Final[str] = "This workspace is out of space, so your change could not be saved."
NOT_SAVED_SENTENCE: Final[str] = "Your change could not be saved. Try again. If this persists, report a bug."


class ShellError(Exception):
    """Base error for everything in the shell subpackage. The message is the developer's reason, which the log,
    agents, and "See details" read; ``user_facing_message``, when set, is the sentence a person is shown in its place,
    ``reportable`` whether that sentence asks for a bug report, and ``kind`` names the refusal for a frontend that
    words it itself."""

    def __init__(
        self,
        message: str = "",
        user_facing_message: str | None = None,
        kind: RefusalKind | None = None,
        reportable: bool | None = None,
    ) -> None:
        self.user_facing_message = user_facing_message
        self.kind = kind
        self.reportable = reportable
        super().__init__(message)


class InvalidShellValueError(ShellError, ValueError):
    """A shell identifier or value does not satisfy its rule."""


class ShellStateError(ShellError, OSError):
    """A shell state file cannot be read or written."""


class ClientNotFoundError(ShellError, LookupError):
    """No client record has the given id."""


class NoTargetClientError(ShellError, ValueError):
    """An op could not be settled on exactly one client (answered 412)."""


class LayoutOpError(ShellError, ValueError):
    """An op's arguments cannot be applied to the arrangement."""


class NoRequesterWindowError(LayoutOpError):
    """``self`` or ``pinned`` names a window of the requester's, and the op carries no requester that has one."""


class UnknownAppError(ShellError, LookupError):
    """No registered app has the given name."""


class NoMessageHandlerError(ShellError, LookupError):
    """No registered app handles the relayed message's type (answered 404)."""


class AppLifecycleRefusedError(ShellError, ValueError):
    """The app cannot be stopped or started through the workspace."""


class SupervisorProgramActionError(ShellError, RuntimeError):
    """Supervisord refused, or could not be reached for, a stop or start."""


class ShareGrantsError(ShellError, ValueError):
    """The share grants document is one the gateway would refuse (it then admits nobody)."""


class DesktopNotFoundError(ShellError, LookupError):
    """No desktop has the given id (or name)."""

    def __init__(self, desktop: str) -> None:
        self.desktop = desktop
        super().__init__(f"Desktop '{desktop}' not found", user_facing_message=DESKTOP_GONE_SENTENCE)


class DesktopConflictError(ShellError, ValueError):
    """A new desktop's id collides with an existing desktop."""


class LastDesktopError(ShellError, ValueError):
    """The last remaining desktop cannot be deleted (answered 409)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, user_facing_message=LAST_DESKTOP_SENTENCE)


class DesktopValueError(ShellError, ValueError):
    """A desktop's name, colour, glyph, shortcut, wallpaper, or a window's app or path is not usable."""


class LaunchFailureError(ShellError):
    """A launch that opened nothing. The message is for the log and "See details". ``user_facing_message`` is a
    sentence the app itself wrote for people about it, passed on as written, and ``reportable`` the app's own word on
    whether that sentence asks for a bug report; both are None when the app gave none (the frontends word it then)."""

    def __init__(self, message: str, user_facing_message: str | None = None, reportable: bool | None = None) -> None:
        super().__init__(message, user_facing_message=user_facing_message, reportable=reportable)


class LaunchRefusedError(LaunchFailureError, ValueError):
    """The app refused a POST launch (a 4xx with its own detail), or the launch's params are not what the path
    declares (answered 400)."""


class LaunchUnavailableError(LaunchFailureError, RuntimeError):
    """A POST launch could not be completed: the app could not be reached, timed out, or answered something other
    than a page path (answered 502)."""


class WindowNotFoundError(ShellError, LookupError):
    """No window on the desktop has the given id."""

    def __init__(self, window: str, user_facing_message: str | None = None, kind: RefusalKind | None = None) -> None:
        self.window = window
        super().__init__(f"Window '{window}' not found", user_facing_message=user_facing_message, kind=kind)


class WindowPoppedOutError(ShellError, ValueError):
    """An op would change where a window the target client has popped out into its own window sits, and the op did
    not carry ``force`` (answered 423): the pop-out is the user's arrangement."""

    def __init__(self, op: str, window: str) -> None:
        self.op = op
        self.window = window
        super().__init__(
            f"Window {window} is popped out into its own window, so {op!r} would bring it back onto the desktop; "
            "pass force to do so anyway"
        )


class PinnedWindowError(ShellError, ValueError):
    """A pinned window cannot be closed (answered 409); minimizing is how it leaves the screen."""


class StalePlacementsSaveError(ShellError, ValueError):
    """A browser's placements save is based on an older layout than the one stored (answered 409)."""


class WallpaperNotFoundError(ShellError, LookupError):
    """No bundled or file wallpaper has the given kind and name."""


class GridSearchExhaustedError(ShellError, AssertionError):
    """The unbounded nearest-free-cell search ran out of rings without finding a free cell, which cannot happen."""


class UpdateNoticeRecordError(ShellError, ValueError):
    """The kept rollback point's file does not hold a record."""


class UpdateNoticeRefusedError(ShellError, ValueError):
    """The update notice is not in a state the verb applies to: nothing kept, a rollback already running, or one done."""


class UpdateNoticeCommandError(ShellError, RuntimeError):
    """The update-self script behind a notice verb could not be run, or failed."""


class PortParkingError(ShellError, OSError):
    """A stopped app's port could not be parked."""


class PortInUseError(PortParkingError):
    """Something already listens on the port the shell meant to park (the app itself, most likely)."""
