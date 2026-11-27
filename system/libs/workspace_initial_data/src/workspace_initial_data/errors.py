class InitialDataError(Exception):
    """Base class for everything this library raises."""


class InitialDataPartRefusedError(InitialDataError):
    """A part of the initial data, or the document as a whole, could not be written; the message says why and names no secret."""


class InitialDataRecordUnreadableError(InitialDataError):
    """The record of the initial data is there and cannot be read, so nobody knows which parts were written."""
