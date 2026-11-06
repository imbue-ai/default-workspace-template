class ChatAppError(Exception):
    """Base error for the chat app's own errors; the app answers every subclass with a ``{"detail"}`` body."""
