"""The end of a file, for a log's last part and a finished run's output tail."""

import os
from pathlib import Path


def read_tail(path: Path, max_bytes: int) -> tuple[str, bool]:
    """The file's last ``max_bytes`` decoded with replacement, and whether anything before them was left out."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        is_truncated = size > max_bytes
        handle.seek(max(0, size - max_bytes))
        return handle.read().decode("utf-8", errors="replace"), is_truncated
