"""The end of a log that only grows, for the readers that want its newest lines without reading it whole."""

from pathlib import Path


def read_tail(path: Path, limit_bytes: int) -> list[str]:
    """The lines in the last ``limit_bytes`` of ``path``; a line cut in two at the start is
    dropped. Raises OSError like ``read_text``."""
    with path.open("rb") as file:
        size = file.seek(0, 2)
        start = max(0, size - limit_bytes)
        file.seek(start)
        lines = file.read().decode(errors="replace").splitlines()
    return lines[1:] if start > 0 else lines
