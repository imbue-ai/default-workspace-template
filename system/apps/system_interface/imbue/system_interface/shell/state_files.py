"""The shell's state files: JSON documents under ``data/.state/system_interface/``, written atomically under one lock."""

import contextlib
import json
import os
import threading
from pathlib import Path
from typing import Any
from typing import Final
from typing import TypeVar
from uuid import uuid4

from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.shell.errors import ShellStateError

# Where the shell keeps its state (desktop contracts.md section 4), relative to the workspace root the
# supervised process runs from; ``main.py`` takes ``--state-dir`` so a test can point elsewhere.
DEFAULT_STATE_DIRECTORY: Final[Path] = Path("data/.state/system_interface")

# One process-wide lock serializes every read-modify-write of every shell state file: the
# files are small and the writers are request threads.
STATE_FILES_LOCK: Final[threading.RLock] = threading.RLock()

_DocumentT = TypeVar("_DocumentT", bound=FrozenModel)


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


def parse_versioned_document(
    raw: dict[str, Any] | None, document_type: type[_DocumentT], expected_version: int, path: Path
) -> _DocumentT | None:
    """The document the JSON object read from ``path`` parses to; None when there was no object, when its
    ``version`` is not the expected one, or when it fails validation (the latter two logged)."""
    if raw is None:
        return None
    if raw.get("version") != expected_version:
        logger.warning(
            "Ignored a shell state file of version {!r} at {} (expected {})",
            raw.get("version"),
            path,
            expected_version,
        )
        return None
    try:
        return document_type.model_validate(raw)
    except ValidationError as e:
        logger.warning("Ignored an unreadable shell state file at {}: {}", path, e.errors()[0]["msg"])
        return None


def write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    """Write ``document`` through a same-directory temp file and a rename, so a reader never sees a partial file."""
    temp_path = path.with_name(f"{path.name}.tmp-{uuid4().hex}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.replace(temp_path, path)
    except OSError as e:
        # Best effort: the cleanup must not replace the error being reported (the temp file's
        # directory may be the very thing that is wrong).
        with contextlib.suppress(OSError):
            temp_path.unlink(missing_ok=True)
        raise ShellStateError(f"cannot write shell state file {path}: {e}") from e
