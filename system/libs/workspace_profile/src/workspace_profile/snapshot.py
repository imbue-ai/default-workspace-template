"""The workspace profile snapshot: the account-keyed settings a client materialized into this workspace.

One file, ``data/.state/profile/account.json``, whose content comes only from a client outside the
workspace (the Imbue Studio desktop, or the hosted web chrome): the shell's profile route
(``PUT /api/workspace/profile``) writes the document a client sends, as it is, the boot's
``apply-initial-data`` step writes the one the workspace's initial data carries, and nothing inside
the workspace decides what it holds. It names the workspace's account and owner, and carries the
account-keyed settings the workspace's apps consume as extra keys beside the header fields (the
keybindings, today). It lives under ``data/.state/`` because it is machine state a client rebuilds:
the account is its source of truth.

Reads are tolerant in both directions: an absent or unreadable file resolves from overrides
and defaults alone, and a snapshot a newer client wrote (a higher ``snapshot_format``, or keys
this build does not know) is still resolved from.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import ConfigDict, Field, ValidationError

from workspace_profile.errors import InvalidProfileSnapshotError
from workspace_profile.overrides import write_flat_value_map

# Relative to the workspace root every supervised process runs from.
SNAPSHOT_RELATIVE_PATH: Final[str] = "data/.state/profile/account.json"

# The newest snapshot shape this build understands. A snapshot carrying a higher format was
# written by a newer client; it is still written as sent, and still resolved from.
SUPPORTED_SNAPSHOT_FORMAT: Final[int] = 1

# The pseudo-account a workspace with no association belongs to.
LOCAL_ACCOUNT_ID: Final[str] = "local"

# The header fields every snapshot carries; everything else in the file is a setting value.
SNAPSHOT_HEADER_KEYS: Final[frozenset[str]] = frozenset(
    {"snapshot_format", "account_id", "owner_user_id", "written_at", "written_by"}
)

# The account-keyed setting keys this template's apps consume from the snapshot. A setting is
# added here only when a workspace app reads it; the vendored contract fixture test pins this
# list against what the desktop writes, so a key added on one side fails the other until
# mirrored.
CONSUMED_SETTING_KEYS: Final[tuple[str, ...]] = ("keybindings",)


class WorkspaceProfileSnapshot(FrozenModel):
    """One parsed snapshot: the account header, plus the setting values it carries by key."""

    model_config = ConfigDict(extra="ignore")

    snapshot_format: int = Field(
        default=SUPPORTED_SNAPSHOT_FORMAT, description="The snapshot's shape version"
    )
    account_id: str = Field(
        default=LOCAL_ACCOUNT_ID,
        description="The account the workspace is associated with",
    )
    owner_user_id: str = Field(
        default=LOCAL_ACCOUNT_ID, description="The owner's user id"
    )
    written_at: str = Field(
        default="", description="When the client wrote it (ISO 8601)"
    )
    written_by: str = Field(
        default="", description="The writing client: a desktop's device id, or 'web'"
    )
    value_by_key: dict[str, object] = Field(
        default_factory=dict,
        description="The account-keyed setting values the snapshot carries",
    )


def parse_workspace_profile_snapshot(text: str) -> WorkspaceProfileSnapshot | None:
    """Parse one snapshot document, or None (logged) when it is not a readable object."""
    try:
        raw = json.loads(text)
    except ValueError as e:
        logger.warning("Ignoring an unparseable workspace profile snapshot: {}", e)
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "Ignoring a workspace profile snapshot that is not an object: {!r}",
            str(raw)[:200],
        )
        return None
    header = {
        str(key): value
        for key, value in raw.items()
        if str(key) in SNAPSHOT_HEADER_KEYS
    }
    values = {
        str(key): value
        for key, value in raw.items()
        if str(key) not in SNAPSHOT_HEADER_KEYS
    }
    try:
        return WorkspaceProfileSnapshot.model_validate(
            {**header, "value_by_key": values}
        )
    except ValidationError as e:
        logger.warning(
            "Ignoring a malformed workspace profile snapshot: {}", e.errors()[0]["msg"]
        )
        return None


def load_workspace_profile_snapshot(
    workspace_root: Path,
) -> WorkspaceProfileSnapshot | None:
    """The workspace's snapshot, or None when no client has written one (or it is unreadable).

    A workspace created by an older client has no snapshot; its settings resolve from
    overrides and defaults alone.
    """
    path = workspace_root / SNAPSHOT_RELATIVE_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        logger.warning(
            "Could not read the workspace profile snapshot at {}: {}", path, e
        )
        return None
    return parse_workspace_profile_snapshot(text)


def _is_same_apart_from_when_written(
    stored_text: str, document: Mapping[str, object]
) -> bool:
    """Whether the stored snapshot holds what ``document`` does, the time each was written aside."""
    try:
        stored = json.loads(stored_text)
    except ValueError:
        return False
    if not isinstance(stored, dict):
        return False
    return {key: value for key, value in stored.items() if key != "written_at"} == {
        key: value for key, value in document.items() if key != "written_at"
    }


def write_workspace_profile_snapshot(
    workspace_root: Path, document: Mapping[str, object]
) -> bool:
    """Replace the snapshot with the document a client sent, atomically; whether it was written.

    A document that equals the stored snapshot apart from ``written_at`` is not written: a client sends the
    snapshot whenever it opens the workspace, and the shell watches the file, so a rewrite would announce a
    change that did not happen.

    Only its shape is asked of it, an object with a whole-number ``snapshot_format``
    (``InvalidProfileSnapshotError`` otherwise): a format newer than this build knows is written
    all the same, since the readers tolerate one and a client newer than the template must still
    be able to write. Raises ``ProfileWriteError`` when the file cannot be written.
    """
    snapshot_format = document.get("snapshot_format")
    if isinstance(snapshot_format, bool) or not isinstance(snapshot_format, int):
        raise InvalidProfileSnapshotError(
            "a profile snapshot carries a whole-number snapshot_format"
        )
    path = workspace_root / SNAPSHOT_RELATIVE_PATH
    try:
        stored_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        stored_text = None
    if stored_text is not None and _is_same_apart_from_when_written(
        stored_text, document
    ):
        return False
    write_flat_value_map(path, document)
    return True
