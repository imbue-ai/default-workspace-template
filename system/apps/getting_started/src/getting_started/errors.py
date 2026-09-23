class GettingStartedError(Exception):
    """Base error for the Getting Started app."""


class StateFileError(GettingStartedError, OSError):
    """One of the app's state files cannot be read or written."""


class TemplateCatalogFormatError(GettingStartedError, ValueError):
    """The catalog document is not one this reader understands."""


class ServeError(GettingStartedError):
    """The page server cannot be started or stopped as asked."""


class ShellAnswerError(GettingStartedError, ValueError):
    """The shell answered a request with a body of another shape than the contract gives it."""
