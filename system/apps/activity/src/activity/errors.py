class ActivityError(Exception):
    """Base error for the Activity app."""


class ServeError(ActivityError):
    """The page server cannot be started or stopped as asked."""


class ChatAppUnavailableError(ActivityError, OSError):
    """The chat app could not be reached, or answered with an error or a body of another shape."""


class SupervisorUnavailableError(ActivityError, OSError):
    """supervisord's socket could not be reached, or did not answer with a list of programs."""
