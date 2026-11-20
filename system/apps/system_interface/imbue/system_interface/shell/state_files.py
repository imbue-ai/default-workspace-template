"""The shell's state files: JSON documents under ``data/.state/system_interface/``, written atomically under one lock."""

import contextlib
import json
import os
import threading
from collections.abc import Callable
from collections.abc import Hashable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from typing import TypeVar
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError
from pydantic.config import ExtraValues

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.errors import ShellStateError

# Where the shell keeps its state (desktop contracts.md section 4), relative to the workspace root the
# supervised process runs from; ``main.py`` takes ``--state-dir`` so a test can point elsewhere.
DEFAULT_STATE_DIRECTORY: Final[Path] = Path("data/.state/system_interface")

# One process-wide lock serializes every read-modify-write of every shell state file: the
# files are small and the writers are request threads.
STATE_FILES_LOCK: Final[threading.RLock] = threading.RLock()

# What a state file that cannot be read is renamed to beside itself: ``<name><infix><UTC timestamp>``.
_MOVED_ASIDE_INFIX: Final[str] = ".unreadable-"
_MOVED_ASIDE_TIMESTAMP_FORMAT: Final[str] = "%Y%m%dT%H%M%S.%fZ"

_DocumentT = TypeVar("_DocumentT", bound=FrozenModel)


class UnreadableStateFile(FrozenModel):
    """A state file that is there but cannot be read, and why."""

    reason: str = Field(description="What is wrong with it")


def load_json_object(path: Path) -> dict[str, Any] | UnreadableStateFile | None:
    """The JSON object at ``path``; None when the file is absent, and why not when it is there but holds none."""
    if not path.exists():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return UnreadableStateFile(reason=f"cannot be read as JSON: {e}")
    if not isinstance(parsed, dict):
        return UnreadableStateFile(reason="is not a JSON object")
    return parsed


def read_json_object(path: Path) -> dict[str, Any] | None:
    """The JSON object at ``path``, None when the file is absent or is not an object (logged)."""
    loaded = load_json_object(path)
    if isinstance(loaded, UnreadableStateFile):
        logger.warning("Skipped shell state file {}: it {}", path, loaded.reason)
        return None
    return loaded


def validate_versioned_document(
    raw: dict[str, Any], document_type: type[_DocumentT], expected_version: int, extra: ExtraValues | None
) -> _DocumentT | UnreadableStateFile:
    """The document ``raw`` parses to, or why it does not: its ``version`` is not the expected one, or it fails
    validation. ``extra`` overrides, at every level, what the document's models do with a field they do not know."""
    if raw.get("version") != expected_version:
        return UnreadableStateFile(reason=f"is of version {raw.get('version')!r}, not {expected_version}")
    try:
        return document_type.model_validate(raw, extra=extra)
    except ValidationError as e:
        return UnreadableStateFile(reason=f"does not validate: {e.errors()[0]['msg']}")


def parse_versioned_document(
    raw: dict[str, Any] | None, document_type: type[_DocumentT], expected_version: int, path: Path
) -> _DocumentT | None:
    """The document the JSON object read from ``path`` parses to; None when there was no object, when its
    ``version`` is not the expected one, or when it fails validation (the latter two logged)."""
    if raw is None:
        return None
    validated = validate_versioned_document(raw, document_type, expected_version, extra=None)
    if isinstance(validated, UnreadableStateFile):
        logger.warning("Ignored the shell state file at {}: it {}", path, validated.reason)
        return None
    return validated


def move_unreadable_state_file_aside(path: Path, reason: str) -> Path:
    """Rename the file at ``path``, which cannot be read, to ``<name>.unreadable-<UTC timestamp>`` beside it (logged),
    so writing a new one destroys nothing; raises ShellStateError when it cannot be moved."""
    timestamp = datetime.now(timezone.utc).strftime(_MOVED_ASIDE_TIMESTAMP_FORMAT)
    aside = path.with_name(f"{path.name}{_MOVED_ASIDE_INFIX}{timestamp}")
    try:
        path.rename(aside)
    except OSError as e:
        raise ShellStateError(f"cannot move the unreadable shell state file {path} aside: {e}") from e
    logger.warning("Moved the unreadable shell state file {} aside to {}: it {}", path, aside.name, reason)
    return aside


@pure
def with_unknown_fields_kept(
    written: dict[str, Any],
    written_model: BaseModel,
    stored: dict[str, Any],
    stored_model: BaseModel,
    identity_of: Callable[[BaseModel], Hashable | None],
) -> dict[str, Any]:
    """``written``, the JSON ``written_model`` dumps to, with the fields its models do not know put back from
    ``stored``, the JSON ``stored_model`` was read from with those fields ignored. They go back at every level: into
    a nested object both hold, and into an item of a list of objects that ``stored`` holds an item of the same
    identity (by ``identity_of``; None matches nothing). Any other value is written as it is."""
    known_fields = type(written_model).model_fields
    kept = {**written, **{key: value for key, value in stored.items() if key not in known_fields}}
    for name in known_fields:
        kept[name] = _value_with_unknown_fields_kept(
            written[name], getattr(written_model, name), stored.get(name), getattr(stored_model, name), identity_of
        )
    return kept


@pure
def _value_with_unknown_fields_kept(
    written: Any,
    written_value: Any,
    stored: Any,
    stored_value: Any,
    identity_of: Callable[[BaseModel], Hashable | None],
) -> Any:
    if isinstance(written_value, BaseModel) and isinstance(stored_value, BaseModel) and isinstance(stored, dict):
        return with_unknown_fields_kept(written, written_value, stored, stored_value, identity_of)
    if not (isinstance(written_value, tuple) and isinstance(stored_value, tuple) and isinstance(stored, list)):
        return written
    # The stored list was validated item by item, so its JSON and its models line up.
    stored_by_identity: dict[Hashable, tuple[dict[str, Any], BaseModel]] = {}
    for item, item_model in zip(stored, stored_value, strict=True):
        identity = identity_of(item_model) if isinstance(item_model, BaseModel) and isinstance(item, dict) else None
        if identity is not None:
            stored_by_identity[identity] = (item, item_model)
    kept_items: list[Any] = []
    for item, item_model in zip(written, written_value, strict=True):
        stored_item = stored_by_identity.get(identity_of(item_model)) if isinstance(item_model, BaseModel) else None
        kept_items.append(
            with_unknown_fields_kept(item, item_model, stored_item[0], stored_item[1], identity_of)
            if stored_item is not None
            else item
        )
    return kept_items


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
