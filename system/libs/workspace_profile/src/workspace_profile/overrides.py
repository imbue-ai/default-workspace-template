"""The workspace's own setting overrides, and the shell's unsent account changes.

Unlike the snapshot these are not rebuildable from the account, so they live under
``data/system/profile/``, the workspace's home for runtime configuration:

- ``overrides/users/<user_id>.json``: one flat map of setting key to value per user, written
  by the workspace only. A value here wins over the account's for that user in this workspace.
- ``unsent.json``: account-wide changes the shell accepted but has not yet delivered to the
  account (written by the shell only). They overlay the owner's snapshot values so the
  workspace behaves as asked at once, and are retried or discarded from the shell's own screen.

Reads are tolerant: an absent, unreadable, or non-object file is an empty map.
"""

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger

from workspace_profile.errors import ProfileWriteError

PROFILE_DIR_RELATIVE_PATH: Final[str] = "data/system/profile"
USER_OVERRIDES_RELATIVE_DIR: Final[str] = f"{PROFILE_DIR_RELATIVE_PATH}/overrides/users"
UNSENT_CHANGES_RELATIVE_PATH: Final[str] = f"{PROFILE_DIR_RELATIVE_PATH}/unsent.json"


def _load_flat_value_map(path: Path, description: str) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Could not read the {} at {}: {}", description, path, e)
        return {}
    try:
        raw = json.loads(text)
    except ValueError as e:
        logger.warning("Ignoring the unparseable {} at {}: {}", description, path, e)
        return {}
    if not isinstance(raw, dict):
        logger.warning("Ignoring the {} at {}: not an object", description, path)
        return {}
    return {str(key): value for key, value in raw.items()}


def _is_plain_filename_user_id(user_id: str) -> bool:
    """Whether the user id can safely name a file inside the overrides directory.

    The id names the override file, so one carrying a path separator (or ``..``) would
    point the read outside the directory. Identity user ids are opaque broker ids and
    never look like paths, so anything path-shaped is refused rather than resolved.
    """
    return bool(user_id) and "/" not in user_id and "\\" not in user_id and "\x00" not in user_id and user_id not in (".", "..")


def load_user_override_values(workspace_root: Path, user_id: str) -> dict[str, object]:
    """The given user's override values in this workspace, by setting key (empty when none)."""
    if not _is_plain_filename_user_id(user_id):
        logger.warning("Ignoring a profile override lookup for a path-shaped user id: {!r}", user_id)
        return {}
    path = workspace_root / USER_OVERRIDES_RELATIVE_DIR / f"{user_id}.json"
    return _load_flat_value_map(path, "profile override file")


def load_unsent_change_values(workspace_root: Path) -> dict[str, object]:
    """The shell's not-yet-delivered account changes, by setting key (empty when none)."""
    path = workspace_root / UNSENT_CHANGES_RELATIVE_PATH
    return _load_flat_value_map(path, "unsent profile changes file")


def _write_flat_value_map(path: Path, value_by_key: Mapping[str, object]) -> None:
    """Atomically replace one flat map file. Raises ``ProfileWriteError``."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(dict(value_by_key), handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temp_name, path)
        except OSError:
            os.unlink(temp_name)
            raise
    except (OSError, TypeError) as e:
        raise ProfileWriteError(f"Could not write {path}: {e}") from e


def write_user_override_values(workspace_root: Path, user_id: str, value_by_key: Mapping[str, object]) -> None:
    """Replace the given user's override file whole. Raises ``ProfileWriteError``.

    A path-shaped user id is refused the way the loader refuses it, so a write
    can never land outside the overrides directory.
    """
    if not _is_plain_filename_user_id(user_id):
        raise ProfileWriteError(f"Refusing a profile override write for a path-shaped user id: {user_id!r}")
    path = workspace_root / USER_OVERRIDES_RELATIVE_DIR / f"{user_id}.json"
    _write_flat_value_map(path, value_by_key)


def write_unsent_change_values(workspace_root: Path, value_by_key: Mapping[str, object]) -> None:
    """Replace the shell's unsent account changes file whole. Raises ``ProfileWriteError``."""
    _write_flat_value_map(workspace_root / UNSENT_CHANGES_RELATIVE_PATH, value_by_key)
