"""Atomic file writes shared by the workspace's durable JSON stores."""

import os
from pathlib import Path
from uuid import uuid4


def write_json_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a same-directory temp file and rename.

    A plain ``write_text`` truncates before it writes, so a concurrent reader
    (another process inspecting the file, an agent, a test poll) can observe
    an empty or partial file. ``os.replace`` makes the swap atomic on POSIX,
    so readers only ever see the old or the new content in full.
    """
    temp_path = path.with_name(f"{path.name}.tmp-{uuid4().hex}")
    temp_path.write_text(text)
    os.replace(temp_path, path)
