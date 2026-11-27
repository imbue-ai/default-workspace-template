"""A document as a creator writes it, and a stand-in for the workspace's backup setup, for this library's tests."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from workspace_backup_setup.errors import BackupSetupError

from workspace_initial_data.apply import INITIAL_DATA_FILE_NAME

# The token of a backup repository, which no status and no log may ever quote.
STORAGE_TOKEN: Final[str] = "store-token-7f3a91c2"

PROFILE_PART_DOCUMENT: Final[dict[str, Any]] = {
    "snapshot_format": 1,
    "account_id": "account-5f3c",
    "owner_user_id": "user-owner-9c21",
    "written_at": "2026-11-26T09:00:00+00:00",
    "written_by": "web",
    "keybindings": {},
}
UPDATE_SETTINGS_PART_DOCUMENT: Final[dict[str, Any]] = {
    "channel": "stable",
    "feed_base_url": "https://updates.example",
    "is_auto_update_enabled": True,
    "window_start_hour": 2,
    "window_end_hour": 5,
}
BACKUPS_PART_DOCUMENT: Final[dict[str, Any]] = {
    "destination": "IMBUE_CLOUD",
    "imbue_cloud_storage": {
        "repository_url": "rest:https://store.example/user/workspace/",
        "rest_username": "workspace",
        "rest_password": STORAGE_TOKEN,
    },
}


def initial_data_document(**members: Any) -> dict[str, Any]:
    """A document of schema 1 carrying all four parts, with ``members`` over them."""
    return {
        "schema": 1,
        "profile": PROFILE_PART_DOCUMENT,
        "timezone": {"zone": "Europe/Berlin"},
        "update_settings": UPDATE_SETTINGS_PART_DOCUMENT,
        "backups": BACKUPS_PART_DOCUMENT,
        **members,
    }


def write_initial_data_document(host_dir: Path, document: object) -> Path:
    """Leave ``document`` in ``host_dir`` as a creator does, and answer where."""
    path = host_dir / INITIAL_DATA_FILE_NAME
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    return path


class RecordingBackupDestination:
    """A stand-in for the function of the backup setup that records a destination: it remembers every request it
    was handed, and refuses the next ones while ``refusal`` is set."""

    def __init__(self) -> None:
        self.requests: list[tuple[Path, dict[str, object]]] = []
        self.refusal: str = ""

    def __call__(self, workspace_root: Path, request: Mapping[str, object]) -> None:
        self.requests.append((workspace_root, dict(request)))
        if self.refusal:
            raise BackupSetupError(self.refusal)
