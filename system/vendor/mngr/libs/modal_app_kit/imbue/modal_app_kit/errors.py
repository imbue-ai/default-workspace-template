class ModalAppKitError(Exception):
    """Base class for the errors this package raises."""


class StructuredRecordMessageError(ModalAppKitError, ValueError):
    """Raised when a structured-record logger is given a message that is not a JSON object."""
