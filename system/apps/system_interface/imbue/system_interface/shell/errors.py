class ShellError(Exception):
    """Base error for everything in the shell subpackage."""


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


class UnknownAppError(ShellError, LookupError):
    """No registered app has the given name."""


class AppLifecycleRefusedError(ShellError, ValueError):
    """The app cannot be stopped or started through the workspace."""


class SupervisorProgramActionError(ShellError, RuntimeError):
    """Supervisord refused, or could not be reached for, a stop or start."""


class DesktopNotFoundError(ShellError, LookupError):
    """No desktop has the given id (or name)."""

    def __init__(self, desktop: str) -> None:
        self.desktop = desktop
        super().__init__(f"Desktop '{desktop}' not found")


class DesktopConflictError(ShellError, ValueError):
    """A new desktop's id collides with an existing desktop."""


class LastDesktopError(ShellError, ValueError):
    """The last remaining desktop cannot be deleted (answered 409)."""


class DesktopValueError(ShellError, ValueError):
    """A desktop's name, colour, glyph, shortcut, wallpaper, or a window's app or path is not usable."""


class WindowNotFoundError(ShellError, LookupError):
    """No window on the desktop has the given id."""

    def __init__(self, window: str) -> None:
        self.window = window
        super().__init__(f"Window '{window}' not found")


class StalePlacementsSaveError(ShellError, ValueError):
    """A browser's placements save is based on an older layout than the one stored (answered 409)."""


class WallpaperNotFoundError(ShellError, LookupError):
    """No bundled or file wallpaper has the given kind and name."""


class GridSearchExhaustedError(ShellError, AssertionError):
    """The unbounded nearest-free-cell search ran out of rings without finding a free cell, which cannot happen."""
