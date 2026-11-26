import os
import time
from pathlib import Path

import pytest
from host_backup.config import RESTIC_ENV_PATH, parse_restic_env_file
from workspace_backup_setup.env_files import (
    PENDING_ENV_RELATIVE_PATH,
    BackupDestinationKind,
    read_pending_env,
)
from workspace_backup_setup.program import BackupSetupProgram
from workspace_backup_setup.state import (
    SETUP_STATE_RELATIVE_PATH,
    BackupSetupState,
    BackupState,
    read_backup_state,
    read_setup_record,
)
from workspace_backup_setup.testing import (
    IMBUE_CLOUD_REPOSITORY,
    WORKSPACE_ID,
    imbue_cloud_request,
    own_storage_request,
)
from workspace_bare_scripts.backup_lease import release_lease, take_lease
from workspace_profile.timezone import read_user_timezone

from workspace_initial_data.cli import HOST_DIR_ENV_VAR, main
from workspace_initial_data.status import (
    STATUS_RELATIVE_PATH,
    InitialDataState,
    InitialDataStatus,
    read_initial_data_status,
)
from workspace_initial_data.testing import (
    STORAGE_TOKEN,
    initial_data_document,
    write_initial_data_document,
)

_STORAGE = {
    "RESTIC_REPOSITORY": IMBUE_CLOUD_REPOSITORY,
    "RESTIC_REST_USERNAME": WORKSPACE_ID,
    "RESTIC_REST_PASSWORD": STORAGE_TOKEN,
}


def _backup_state(workspace_root: Path, lease_path: Path) -> BackupState:
    return read_backup_state(
        workspace_root,
        lease_path=lease_path,
        now=time.time(),
        workspace_id=WORKSPACE_ID,
    )


def _applied(workspace_root: Path) -> InitialDataStatus:
    main()
    return read_initial_data_status(workspace_root)


def test_the_step_applies_the_document_in_the_host_directory_to_the_workspace_it_runs_from(
    workspace_root: Path, host_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HOST_DIR_ENV_VAR, str(host_dir))
    document_path = write_initial_data_document(
        host_dir, initial_data_document(backups=None)
    )

    main()

    assert read_initial_data_status(workspace_root).state is InitialDataState.APPLIED
    assert read_user_timezone(workspace_root) == "Europe/Berlin"
    assert not document_path.exists()


def test_the_step_does_nothing_where_no_host_directory_is_named(
    workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(HOST_DIR_ENV_VAR, raising=False)

    main()

    assert not (workspace_root / "data").exists()


@pytest.mark.usefixtures("boot_environment", "fake_restic_control")
def test_imbue_cloud_with_storage_is_recorded_for_the_backup_setup_which_then_installs_it(
    workspace_root: Path, host_dir: Path, lease_path: Path
) -> None:
    document_path = write_initial_data_document(
        host_dir, initial_data_document(backups=imbue_cloud_request(STORAGE_TOKEN))
    )

    status = _applied(workspace_root)

    assert status.state is InitialDataState.APPLIED
    assert not document_path.exists()
    recorded = _backup_state(workspace_root, lease_path)
    assert (recorded.destination, recorded.setup.state, recorded.repository) == (
        BackupDestinationKind.IMBUE_CLOUD,
        BackupSetupState.INITIALIZING,
        IMBUE_CLOUD_REPOSITORY,
    )
    # The step installs nothing: the storage waits, with no password yet, for backup-setup.
    assert recorded.is_configured is False
    request_id = read_setup_record(workspace_root).request_id
    assert read_pending_env(workspace_root, request_id) == _STORAGE
    assert STORAGE_TOKEN not in (workspace_root / STATUS_RELATIVE_PATH).read_text()

    BackupSetupProgram(
        workspace_root,
        lease_path=lease_path,
        clock=time.time,
        workspace_id=WORKSPACE_ID,
        make_password=lambda: "made-by-the-workspace",
    ).run_pass()

    ready = _backup_state(workspace_root, lease_path)
    assert (ready.destination, ready.setup.state, ready.is_configured) == (
        BackupDestinationKind.IMBUE_CLOUD,
        BackupSetupState.READY,
        True,
    )
    assert parse_restic_env_file((workspace_root / RESTIC_ENV_PATH).read_text()) == {
        **_STORAGE,
        "RESTIC_PASSWORD": "made-by-the-workspace",
    }


@pytest.mark.usefixtures("boot_environment")
def test_imbue_cloud_with_no_storage_reads_as_awaiting_it(
    workspace_root: Path, host_dir: Path, lease_path: Path
) -> None:
    write_initial_data_document(
        host_dir, initial_data_document(backups={"destination": "IMBUE_CLOUD"})
    )

    status = _applied(workspace_root)

    assert status.state is InitialDataState.APPLIED
    recorded = _backup_state(workspace_root, lease_path)
    assert (recorded.destination, recorded.setup.state) == (
        BackupDestinationKind.IMBUE_CLOUD,
        BackupSetupState.AWAITING_STORAGE,
    )


@pytest.mark.usefixtures("boot_environment")
def test_none_is_applied_and_leaves_the_backups_off(
    workspace_root: Path, host_dir: Path, lease_path: Path
) -> None:
    document_path = write_initial_data_document(
        host_dir, initial_data_document(backups={"destination": "NONE"})
    )

    status = _applied(workspace_root)

    assert status.state is InitialDataState.APPLIED
    assert not document_path.exists()
    recorded = _backup_state(workspace_root, lease_path)
    assert (recorded.destination, recorded.setup.state, recorded.is_configured) == (
        BackupDestinationKind.NONE,
        BackupSetupState.NONE,
        False,
    )
    assert not (workspace_root / PENDING_ENV_RELATIVE_PATH).exists()


@pytest.mark.parametrize(
    ("backups", "said"),
    [
        ({"destination": "TAPE"}, "'TAPE' is not a destination this workspace knows"),
        (
            own_storage_request(
                "s3:https://s3.example/bucket", {"RESTIC_PASSWORD": STORAGE_TOKEN}
            ),
            "RESTIC_PASSWORD must not be among the storage's variables",
        ),
        (
            {**imbue_cloud_request(STORAGE_TOKEN), "destination": "NONE"},
            "NONE takes neither",
        ),
    ],
)
@pytest.mark.usefixtures("boot_environment")
def test_a_destination_the_backup_setup_refuses_fails_that_part_and_the_others_are_written(
    workspace_root: Path,
    host_dir: Path,
    lease_path: Path,
    backups: dict[str, object],
    said: str,
) -> None:
    document_path = write_initial_data_document(
        host_dir, initial_data_document(backups=backups)
    )

    status = _applied(workspace_root)

    assert status.state is InitialDataState.FAILED
    assert status.detail.startswith("backups: ")
    assert said in status.detail
    assert STORAGE_TOKEN not in status.detail
    assert document_path.exists()
    assert read_user_timezone(workspace_root) == "Europe/Berlin"
    assert not (workspace_root / SETUP_STATE_RELATIVE_PATH).exists()
    assert (
        _backup_state(workspace_root, lease_path).destination
        is BackupDestinationKind.NONE
    )


@pytest.mark.usefixtures("boot_environment")
def test_a_destination_named_while_a_backup_operation_runs_fails_and_the_next_start_records_it(
    workspace_root: Path, host_dir: Path, lease_path: Path
) -> None:
    document_path = write_initial_data_document(
        host_dir, initial_data_document(backups=imbue_cloud_request(STORAGE_TOKEN))
    )
    take_lease(lease_path, "restore", time.time())

    held = _applied(workspace_root)

    assert held.state is InitialDataState.FAILED
    assert held.detail.startswith("backups: A backup operation is running (restore)")
    assert document_path.exists()
    assert not (workspace_root / PENDING_ENV_RELATIVE_PATH).exists()

    release_lease(lease_path, "restore", time.time())
    again = _applied(workspace_root)

    assert again.state is InitialDataState.APPLIED
    assert not document_path.exists()
    request_id = read_setup_record(workspace_root).request_id
    assert read_pending_env(workspace_root, request_id) == _STORAGE


@pytest.mark.usefixtures("boot_environment")
def test_a_document_applied_twice_asks_the_backup_setup_for_nothing_new(
    workspace_root: Path, host_dir: Path, lease_path: Path
) -> None:
    # Another part is refused, so the document stays and the next start applies it again whole.
    document = initial_data_document(
        backups=imbue_cloud_request(STORAGE_TOKEN), timezone={"zone": "Nowhere/Land"}
    )
    document_path = write_initial_data_document(host_dir, document)

    first = _applied(workspace_root)

    assert first.state is InitialDataState.FAILED
    assert first.detail.startswith("timezone: ")
    record_text = (workspace_root / SETUP_STATE_RELATIVE_PATH).read_text()
    pending = workspace_root / PENDING_ENV_RELATIVE_PATH
    pending_text, pending_inode = pending.read_text(), os.stat(pending).st_ino

    # A lease held now would refuse a request that changed anything, and this one changes nothing.
    take_lease(lease_path, "restore", time.time())
    second = _applied(workspace_root)

    assert second == first
    assert document_path.exists()
    assert (workspace_root / SETUP_STATE_RELATIVE_PATH).read_text() == record_text
    assert (pending.read_text(), os.stat(pending).st_ino) == (
        pending_text,
        pending_inode,
    )
