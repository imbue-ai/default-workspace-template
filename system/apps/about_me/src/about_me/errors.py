class AboutMeError(Exception):
    """Base class for this app's errors."""


class StaleFactError(AboutMeError):
    """The fact a request names is no longer in the profile as the page saw it: the file changed since."""
