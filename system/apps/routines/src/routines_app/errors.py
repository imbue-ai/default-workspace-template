class RoutinesAppError(Exception):
    """Base exception for the Routines app."""


class FixChatError(RoutinesAppError, RuntimeError):
    """The Fix chat could not be started."""
