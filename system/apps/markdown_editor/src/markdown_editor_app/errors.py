class MarkdownEditorError(Exception):
    """Base class for the Markdown Editor's own errors."""


class FileRefusedError(MarkdownEditorError):
    """A request for a file that cannot be honoured; ``status`` is the HTTP status the route answers it with."""

    def __init__(self, detail: str, status: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status
