"""The error a preview flow raises, apart from ``preview_app`` so the script run as ``__main__`` and the
modules it imports raise and catch the same class."""


class PreviewError(Exception):
    """A preview could not be described or booted."""
