import time
from collections.abc import Mapping
from pathlib import Path

import pytest
from host_backup.config import RESTIC_ENV_PATH, parse_restic_env_file
from workspace_backup_setup.destination import request_backup_destination
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

from workspace_initial_data.apply import apply_initial_data
from workspace_initial_data.cli import HOST_DIR_ENV_VAR, main, record_backup_destination
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
        # A line typed `NAME: value` reaches the workspace as a name that holds the value.
        (
            own_storage_request(
                "s3:https://s3.example/bucket",
                {f"AZURE_ACCOUNT_KEY: {STORAGE_TOKEN}": "="},
            ),
            "not the name of an environment variable",
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
    assert STORAGE_TOKEN not in (workspace_root / STATUS_RELATIVE_PATH).read_text()
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


def _turn_backups_off(workspace_root: Path, lease_path: Path) -> None:
    """What the destination's route does when the owner turns backups off."""
    request_backup_destination(
        workspace_root,
        {"destination": "NONE"},
        lease_path=lease_path,
        now=time.time(),
        workspace_id=WORKSPACE_ID,
    )


@pytest.mark.usefixtures("boot_environment")
def test_backups_the_owner_turned_off_stay_off_when_the_document_is_tried_again(
    workspace_root: Path, host_dir: Path, lease_path: Path
) -> None:
    # Another part is refused, so the document stays and the next start tries that part again.
    document = initial_data_document(
        backups=imbue_cloud_request(STORAGE_TOKEN), timezone={"zone": 7}
    )
    document_path = write_initial_data_document(host_dir, document)

    first = _applied(workspace_root)

    assert first.state is InitialDataState.FAILED
    assert first.detail.startswith("timezone: ")
    assert (
        _backup_state(workspace_root, lease_path).destination
        is BackupDestinationKind.IMBUE_CLOUD
    )

    _turn_backups_off(workspace_root, lease_path)
    second = _applied(workspace_root)

    assert second == first
    assert document_path.exists()
    off = _backup_state(workspace_root, lease_path)
    assert (off.destination, off.setup.state) == (
        BackupDestinationKind.NONE,
        BackupSetupState.NONE,
    )
    assert not (workspace_root / PENDING_ENV_RELATIVE_PATH).exists()


class _StepKilled(BaseException):
    """Stands in for the step being killed in the middle of a part."""


@pytest.mark.usefixtures("boot_environment")
def test_backups_turned_off_after_the_step_was_killed_recording_them_stay_off(
    workspace_root: Path, host_dir: Path, lease_path: Path
) -> None:
    document_path = write_initial_data_document(
        host_dir, initial_data_document(backups=imbue_cloud_request(STORAGE_TOKEN))
    )

    def _record_the_destination_and_die(
        root: Path, request: Mapping[str, object]
    ) -> object:
        record_backup_destination(root, request)
        raise _StepKilled()

    # The boot goes on when the step dies, so the services run and the owner reaches the routes.
    with pytest.raises(_StepKilled):
        apply_initial_data(
            host_dir, workspace_root, _record_the_destination_and_die, time.time()
        )
    assert (
        _backup_state(workspace_root, lease_path).destination
        is BackupDestinationKind.IMBUE_CLOUD
    )
    _turn_backups_off(workspace_root, lease_path)

    at_the_next_start = _applied(workspace_root)

    assert at_the_next_start.state is InitialDataState.FAILED
    assert at_the_next_start.detail.startswith(
        "backups: this part was being written when the step was interrupted"
    )
    assert (
        _backup_state(workspace_root, lease_path).destination
        is BackupDestinationKind.NONE
    )
    assert not (workspace_root / PENDING_ENV_RELATIVE_PATH).exists()
    assert not document_path.exists()
