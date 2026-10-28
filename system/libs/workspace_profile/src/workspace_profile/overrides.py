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
from pathlib import Path
from typing import Final

from loguru import logger

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


def load_user_override_values(workspace_root: Path, user_id: str) -> dict[str, object]:
    """The given user's override values in this workspace, by setting key (empty when none)."""
    path = workspace_root / USER_OVERRIDES_RELATIVE_DIR / f"{user_id}.json"
    return _load_flat_value_map(path, "profile override file")


def load_unsent_change_values(workspace_root: Path) -> dict[str, object]:
    """The shell's not-yet-delivered account changes, by setting key (empty when none)."""
    path = workspace_root / UNSENT_CHANGES_RELATIVE_PATH
    return _load_flat_value_map(path, "unsent profile changes file")
