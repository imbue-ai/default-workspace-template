"""The workspace profile snapshot: the account-keyed settings a client materialized into this workspace.

One file, ``data/.state/profile/account.json``, written only by a client outside the workspace
(the Imbue Studio desktop, or the hosted web chrome) and never by anything inside it. It names the
workspace's account and owner, and carries the account-keyed settings the workspace's apps
consume as extra keys beside the header fields (the keybindings, today). It lives under
``data/.state/`` because it is machine state a client rebuilds: the account is its source of
truth.

Reads are tolerant in both directions: an absent or unreadable file resolves from overrides
and defaults alone, and a snapshot a newer client wrote (a higher ``snapshot_format``, or keys
this build does not know) is still resolved from.
"""

import json
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import ConfigDict, Field, ValidationError

# Relative to the workspace root every supervised process runs from.
SNAPSHOT_RELATIVE_PATH: Final[str] = "data/.state/profile/account.json"

# The newest snapshot shape this build understands. A snapshot carrying a higher format was
# written by a newer client; it is still resolved from (the workspace never writes it anyway).
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
