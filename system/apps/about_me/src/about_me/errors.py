class AboutMeError(Exception):
    """Base class for this app's errors."""


class InvalidFactError(AboutMeError):
    """The fact the user typed cannot be kept as one line of the memory: it is empty, too long, or a comment marker."""


class StaleFactError(AboutMeError):
    """The fact a request names is no longer in the profile as the page saw it: the file changed since."""
