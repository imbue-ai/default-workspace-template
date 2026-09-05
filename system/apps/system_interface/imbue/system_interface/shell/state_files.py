"""The shell's state files: JSON documents under ``data/.state/system_interface/``, written atomically under one lock."""

import json
import os
import threading
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

from loguru import logger

from imbue.system_interface.shell.errors import ShellStateError

# Where the shell keeps its state (contracts.md section 7), relative to the workspace root the
# supervised process runs from; ``main.py`` takes ``--state-dir`` so a test can point elsewhere.
DEFAULT_STATE_DIRECTORY: Final[Path] = Path("data/.state/system_interface")

# One process-wide lock serializes every read-modify-write of every shell state file, exactly
# as the old projects module did: the files are small and the writers are request threads.
STATE_FILES_LOCK: Final[threading.RLock] = threading.RLock()


def read_json_object(path: Path) -> dict[str, Any] | None:
    """The JSON object at ``path``, None when the file is absent or is not an object (logged)."""
    if not path.exists():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.opt(exception=e).warning("Skipped unreadable shell state file {}", path)
        return None
    if not isinstance(parsed, dict):
        logger.warning("Skipped shell state file {}: expected a JSON object", path)
        return None
    return parsed


def write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    """Write ``document`` through a same-directory temp file and a rename, so a reader never sees a partial file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.tmp-{uuid4().hex}")
        temp_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.replace(temp_path, path)
    except OSError as e:
        raise ShellStateError(f"cannot write shell state file {path}: {e}") from e
