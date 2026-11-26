class InitialDataError(Exception):
    """Base class for everything this library raises."""


class InitialDataPartRefusedError(InitialDataError):
    """A part of the initial data could not be written; the message says which part and why, and names no secret."""
