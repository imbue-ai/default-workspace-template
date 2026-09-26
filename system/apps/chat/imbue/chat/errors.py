class ChatAppError(Exception):
    """Base error for the chat app's own errors; the app answers every subclass with a ``{"detail"}`` body."""


class MalformedRequestError(ChatAppError):
    """The request body is not JSON, not an object, or not the route's shape (answered 400)."""
