"""The app's state files: JSON documents under ``data/.state/getting-started/`` (the catalog's last good copy and the
first-visit ledger), each written atomically."""

import contextlib
import json
import os
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

from loguru import logger

from getting_started.errors import StateFileError

# Where the app keeps what it knows about this machine (workspace app model contracts.md section 17), relative to
# the workspace root the supervised process runs from; ``main.py`` takes ``--state-dir`` so a test can point elsewhere.
DEFAULT_STATE_DIRECTORY: Final[Path] = Path("data/.state/getting-started")


def read_json_object(path: Path) -> dict[str, Any] | None:
    """The JSON object at ``path``, None when the file is absent or is not an object (logged)."""
    if not path.exists():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.opt(exception=e).warning("Skipped unreadable state file {}", path)
        return None
    if not isinstance(parsed, dict):
        logger.warning("Skipped state file {}: expected a JSON object", path)
        return None
    return parsed


def write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    """Write ``document`` through a same-directory temp file and a rename, so a reader never sees a partial file."""
    temp_path = path.with_name(f"{path.name}.tmp-{uuid4().hex}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.replace(temp_path, path)
    except OSError as e:
        with contextlib.suppress(OSError):
            temp_path.unlink(missing_ok=True)
        raise StateFileError(f"cannot write state file {path}: {e}") from e
