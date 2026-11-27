"""Holds this library's reader to the documents the Imbue Studio desktop writes.

The desktop builds the document and this library applies it, across two repos. The desktop publishes
``workspace_initial_data_contract.json`` beside its builder (``apps/minds/imbue/minds/desktop_client/`` in mngr):
the documents it builds for one set of inputs, which its own tests hold the builder to. The file beside this
test is that one byte for byte, and this test applies each of its documents, so a member renamed on one side
fails the other until it is mirrored.
"""

import json
import time
from pathlib import Path
from typing import Any, Final

import pytest
from workspace_backup_setup.state import read_backup_state
from workspace_backup_setup.testing import WORKSPACE_ID
from workspace_profile.snapshot import SNAPSHOT_RELATIVE_PATH
from workspace_profile.timezone import read_user_timezone
from workspace_update_control.settings import read_update_settings

from workspace_initial_data.apply import (
    BACKUPS_PART,
    INITIAL_DATA_FILE_NAME,
    INITIAL_DATA_SCHEMA,
    PROFILE_PART,
    TIMEZONE_PART,
    UPDATE_SETTINGS_PART,
    apply_initial_data,
)
from workspace_initial_data.cli import record_backup_destination
from workspace_initial_data.status import applied_status, read_initial_data_status

_NOW: Final[float] = 1_795_000_000.0
_CONTRACT: Final[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "workspace_initial_data_contract.json").read_text(
        encoding="utf-8"
    )
)
_PARTS: Final[frozenset[str]] = frozenset(
    {PROFILE_PART, TIMEZONE_PART, UPDATE_SETTINGS_PART, BACKUPS_PART}
)


def test_the_contract_names_the_file_and_the_schema_this_library_reads() -> None:
    assert _CONTRACT["file_name"] == INITIAL_DATA_FILE_NAME
    assert _CONTRACT["schema"] == INITIAL_DATA_SCHEMA
    # The boot looks in the machine's host directory, so only the name of the remote path is this library's.
    assert Path(_CONTRACT["remote_path"]).name == INITIAL_DATA_FILE_NAME


@pytest.mark.parametrize("name", sorted(_CONTRACT["documents"]))
@pytest.mark.usefixtures("boot_environment")
def test_each_document_the_desktop_writes_is_applied_part_by_part(
    workspace_root: Path, host_dir: Path, lease_path: Path, name: str
) -> None:
    document = _CONTRACT["documents"][name]
    document_path = host_dir / _CONTRACT["file_name"]
    document_path.write_text(json.dumps(document), encoding="utf-8")

    status = apply_initial_data(
        host_dir, workspace_root, record_backup_destination, _NOW
    )

    assert status == applied_status(_NOW)
    assert read_initial_data_status(workspace_root) == status
    assert not document_path.exists()
    # Every member the desktop writes beside the schema is a part this library hands to a writer.
    assert set(document) - {"schema"} <= _PARTS
    assert (
        json.loads((workspace_root / SNAPSHOT_RELATIVE_PATH).read_text())
        == document[PROFILE_PART]
    )
    assert read_user_timezone(workspace_root) == document.get(TIMEZONE_PART, {}).get(
        "zone", ""
    )
    settings = read_update_settings()
    assert settings is not None
    assert {
        "channel": settings.channel,
        "feed_base_url": settings.feed_base_url,
        "is_auto_update_enabled": settings.is_auto_update_enabled,
        "window_start_hour": settings.window_start_hour,
        "window_end_hour": settings.window_end_hour,
    } == document[UPDATE_SETTINGS_PART]
    backups = read_backup_state(
        workspace_root,
        lease_path=lease_path,
        now=time.time(),
        workspace_id=WORKSPACE_ID,
    )
    assert backups.destination.value == document.get(BACKUPS_PART, {}).get(
        "destination", "NONE"
    )
