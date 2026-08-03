class ShareRelayError(Exception):
    """Base exception for all share_relay errors."""


class InvalidRelayConfigurationError(ShareRelayError, ValueError):
    """Raised for an invalid RelayConfiguration field value.

    Subclasses ValueError so pydantic field validators can raise it and have it
    surface as a normal ValidationError.
    """
