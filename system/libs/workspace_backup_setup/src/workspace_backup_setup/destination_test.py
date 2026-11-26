import stat
from pathlib import Path

import pytest
from host_backup.config import RESTIC_ENV_PATH, parse_restic_env_file
from workspace_bare_scripts.backup_lease import HOLDER_SETUP, release_lease, take_lease

from workspace_backup_setup.destination import request_backup_destination
from workspace_backup_setup.env_files import (
    PENDING_ENV_RELATIVE_PATH,
    BackupDestinationKind,
    kept_envs,
    read_pending_env,
    write_pending_env,
)
from workspace_backup_setup.errors import (
    BackupOperationRunningError,
    InvalidBackupDestinationError,
)
from workspace_backup_setup.state import (
    SETUP_STATE_RELATIVE_PATH,
    BackupSetupState,
    BackupState,
    failed_record,
    read_backup_state,
    read_setup_record,
    write_setup_record,
)
from workspace_backup_setup.testing import (
    IMBUE_CLOUD_REPOSITORY,
    WORKSPACE_ID,
    imbue_cloud_request,
    own_storage_request,
)

_NOW = 1_795_000_000.0


def _request(
    workspace_root: Path, lease_path: Path, body: dict[str, object], now: float = _NOW
) -> tuple[BackupState, bool]:
    outcome = request_backup_destination(
        workspace_root, body, lease_path=lease_path, now=now, workspace_id=WORKSPACE_ID
    )
    return outcome.state, outcome.is_setup_begun


def _state(workspace_root: Path, lease_path: Path, now: float = _NOW) -> BackupState:
    return read_backup_state(
        workspace_root, lease_path=lease_path, now=now, workspace_id=WORKSPACE_ID
    )


def test_a_workspace_nothing_was_asked_of_has_no_destination(
    workspace_root: Path, lease_path: Path
) -> None:
    state = _state(workspace_root, lease_path)

    assert state.model_dump(mode="json") == {
        "destination": "NONE",
        "setup": {
            "state": "NONE",
            "detail": "",
            "started_at": None,
            "finished_at": None,
            "retry_at": None,
        },
        "is_configured": False,
        "is_verification_enabled": True,
        "repository": "",
        "credentials_digest": "",
        "lease": None,
    }


def test_storage_named_for_a_destination_is_kept_for_backup_setup_and_installs_nothing(
    workspace_root: Path, lease_path: Path
) -> None:
    state, is_setup_begun = _request(
        workspace_root, lease_path, imbue_cloud_request("token-a")
    )

    assert is_setup_begun is True
    assert (state.destination, state.setup.state) == (
        BackupDestinationKind.IMBUE_CLOUD,
        BackupSetupState.INITIALIZING,
    )
    assert (state.repository, state.is_configured, state.credentials_digest) == (
        IMBUE_CLOUD_REPOSITORY,
        False,
        "",
    )
    assert not (workspace_root / RESTIC_ENV_PATH).exists()
    pending = workspace_root / PENDING_ENV_RELATIVE_PATH
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    assert parse_restic_env_file(pending.read_text()) == {
        "RESTIC_REPOSITORY": IMBUE_CLOUD_REPOSITORY,
        "RESTIC_REST_USERNAME": WORKSPACE_ID,
        "RESTIC_REST_PASSWORD": "token-a",
    }
    assert "token-a" not in (workspace_root / SETUP_STATE_RELATIVE_PATH).read_text()


def test_a_repeat_of_the_same_request_changes_nothing(
    workspace_root: Path, lease_path: Path
) -> None:
    first, _ = _request(workspace_root, lease_path, imbue_cloud_request("token-a"))
    record_text = (workspace_root / SETUP_STATE_RELATIVE_PATH).read_text()
    pending_text = (workspace_root / PENDING_ENV_RELATIVE_PATH).read_text()

    again, is_setup_begun = _request(
        workspace_root, lease_path, imbue_cloud_request("token-a"), now=_NOW + 30.0
    )
    with_no_storage, is_begun_with_no_storage = _request(
        workspace_root, lease_path, {"destination": "IMBUE_CLOUD"}, now=_NOW + 31.0
    )

    assert (is_setup_begun, is_begun_with_no_storage) == (False, False)
    assert again == first == with_no_storage
    assert (workspace_root / SETUP_STATE_RELATIVE_PATH).read_text() == record_text
    assert (workspace_root / PENDING_ENV_RELATIVE_PATH).read_text() == pending_text


def test_a_destination_named_with_no_storage_awaits_it_when_the_workspace_kept_none(
    workspace_root: Path, lease_path: Path
) -> None:
    state, is_setup_begun = _request(
        workspace_root, lease_path, {"destination": "IMBUE_CLOUD"}
    )

    assert is_setup_begun is False
    assert (state.destination, state.setup.state) == (
        BackupDestinationKind.IMBUE_CLOUD,
        BackupSetupState.AWAITING_STORAGE,
    )
    assert (
        state.setup.detail
        == "This workspace holds no storage credentials for the destination."
    )
    assert not (workspace_root / PENDING_ENV_RELATIVE_PATH).exists()


def test_a_destination_named_with_no_storage_is_set_up_from_what_was_kept_for_it(
    workspace_root: Path, lease_path: Path
) -> None:
    secrets_directory = (workspace_root / RESTIC_ENV_PATH).parent
    secrets_directory.mkdir(parents=True)
    (secrets_directory / "restic.env.20260301T101500Z").write_text(
        f"RESTIC_REPOSITORY={IMBUE_CLOUD_REPOSITORY}\nRESTIC_REST_USERNAME={WORKSPACE_ID}\n"
        "RESTIC_REST_PASSWORD=older-token\nRESTIC_PASSWORD=older-password\n"
    )
    (secrets_directory / "restic.env.20260902T080000Z").write_text(
        f"RESTIC_REPOSITORY={IMBUE_CLOUD_REPOSITORY}\nRESTIC_REST_USERNAME={WORKSPACE_ID}\n"
        "RESTIC_REST_PASSWORD=kept-token\nRESTIC_PASSWORD=kept-password\n"
    )
    (secrets_directory / "restic.env.20261001T080000Z").write_text(
        "RESTIC_REPOSITORY=s3:https://s3.example/bucket\nAWS_ACCESS_KEY_ID=key\nRESTIC_PASSWORD=own-password\n"
    )

    state, is_setup_begun = _request(
        workspace_root, lease_path, {"destination": "IMBUE_CLOUD"}
    )

    assert is_setup_begun is True
    assert (state.setup.state, state.repository) == (
        BackupSetupState.INITIALIZING,
        IMBUE_CLOUD_REPOSITORY,
    )
    assert read_pending_env(
        workspace_root, read_setup_record(workspace_root).request_id
    ) == {
        "RESTIC_REPOSITORY": IMBUE_CLOUD_REPOSITORY,
        "RESTIC_REST_USERNAME": WORKSPACE_ID,
        "RESTIC_REST_PASSWORD": "kept-token",
        "RESTIC_PASSWORD": "kept-password",
    }

    own, _ = _request(
        workspace_root, lease_path, {"destination": "OWN_STORAGE"}, now=_NOW + 1.0
    )
    assert (own.destination, own.repository) == (
        BackupDestinationKind.OWN_STORAGE,
        "s3:https://s3.example/bucket",
    )


@pytest.mark.parametrize(
    ("body", "said"),
    [
        ({"destination": "TAPE"}, "'TAPE' is not a destination this workspace knows"),
        ({}, "destination"),
        (
            own_storage_request(
                "s3:https://s3.example/bucket", {"RESTIC_PASSWORD": "typed-password"}
            ),
            "RESTIC_PASSWORD must not be among the storage's variables",
        ),
        (
            own_storage_request(
                "s3:https://s3.example/bucket",
                {"RESTIC_PASSWORD_COMMAND": "cat /etc/passwd"},
            ),
            "RESTIC_PASSWORD_COMMAND must not be among the storage's variables",
        ),
        (
            own_storage_request(
                "s3:https://s3.example/bucket",
                {"RESTIC_REPOSITORY": "s3:https://other.example/b"},
            ),
            "name the repository as `repository`",
        ),
        (
            own_storage_request("s3:https://s3.example/bucket", {"AWS KEY": "v"}),
            "'AWS KEY' is not the name",
        ),
        (
            own_storage_request(
                "s3:https://s3.example/bucket", {"AWS_ACCESS_KEY_ID": "a\nB=c"}
            ),
            "AWS_ACCESS_KEY_ID must be one line",
        ),
        (own_storage_request("", {}), "repository must be one line that is not empty"),
        (
            {
                "destination": "IMBUE_CLOUD",
                "own_storage": {"repository": "s3:https://s3.example/bucket"},
            },
            "The storage sent is not IMBUE_CLOUD's",
        ),
        (
            {**imbue_cloud_request("token-a"), "destination": "NONE"},
            "The storage sent is not NONE's",
        ),
        (
            {
                "destination": "IMBUE_CLOUD",
                "imbue_cloud_storage": {
                    "repository_url": "s3:https://s3.example/bucket",
                    "rest_username": WORKSPACE_ID,
                    "rest_password": "token-a",
                },
            },
            "repository_url must be a rest: address",
        ),
        ({"destination": "NONE", "env_text": "RESTIC_PASSWORD=x"}, "env_text"),
    ],
)
def test_a_destination_the_workspace_cannot_take_is_refused_and_changes_nothing(
    workspace_root: Path, lease_path: Path, body: dict[str, object], said: str
) -> None:
    with pytest.raises(InvalidBackupDestinationError) as refused:
        _request(workspace_root, lease_path, body)

    assert said in str(refused.value)
    assert "typed-password" not in str(refused.value)
    assert not (workspace_root / "data").exists()


def test_a_request_that_differs_is_refused_while_another_backup_operation_holds_the_backup_lease(
    workspace_root: Path, lease_path: Path
) -> None:
    _request(workspace_root, lease_path, imbue_cloud_request("token-a"))
    record_text = (workspace_root / SETUP_STATE_RELATIVE_PATH).read_text()
    taken = take_lease(lease_path, "restore", _NOW + 1.0)

    with pytest.raises(BackupOperationRunningError) as refused:
        _request(workspace_root, lease_path, {"destination": "NONE"}, now=_NOW + 2.0)
    same, is_setup_begun = _request(
        workspace_root, lease_path, imbue_cloud_request("token-a"), now=_NOW + 2.0
    )

    assert refused.value.lease == taken
    assert "restore" in str(refused.value)
    assert (workspace_root / SETUP_STATE_RELATIVE_PATH).read_text() == record_text
    assert is_setup_begun is False
    assert same.lease is not None and same.lease.holder == "restore"

    release_lease(lease_path, "restore", _NOW + 3.0)
    off, _ = _request(
        workspace_root, lease_path, {"destination": "NONE"}, now=_NOW + 4.0
    )
    assert (off.destination, off.setup.state, off.lease) == (
        BackupDestinationKind.NONE,
        BackupSetupState.NONE,
        None,
    )
    assert not (workspace_root / PENDING_ENV_RELATIVE_PATH).exists()


def _begin_an_attempt(workspace_root: Path, lease_path: Path) -> dict[str, str]:
    """Leave the workspace as ``backup-setup`` has it in the middle of an attempt at Imbue Cloud under
    ``token-a``: holding the lease, with the password it made kept beside the storage. Answers what it keeps."""
    _request(workspace_root, lease_path, imbue_cloud_request("token-a"))
    request_id = read_setup_record(workspace_root).request_id
    attempting = {
        **(read_pending_env(workspace_root, request_id) or {}),
        "RESTIC_PASSWORD": "made-password",
    }
    write_pending_env(workspace_root, request_id, attempting)
    take_lease(lease_path, HOLDER_SETUP, _NOW + 1.0)
    return attempting


def test_backups_are_turned_off_while_the_workspaces_own_setup_holds_the_backup_lease(
    workspace_root: Path, lease_path: Path
) -> None:
    attempting = _begin_an_attempt(workspace_root, lease_path)

    off, _ = _request(
        workspace_root, lease_path, {"destination": "NONE"}, now=_NOW + 2.0
    )

    assert (off.destination, off.setup.state) == (
        BackupDestinationKind.NONE,
        BackupSetupState.NONE,
    )
    assert off.lease is not None and off.lease.holder == HOLDER_SETUP
    assert not (workspace_root / PENDING_ENV_RELATIVE_PATH).exists()
    # The repository may already be initialised with the password the attempt made, so it is kept.
    assert kept_envs(workspace_root) == [attempting]


def test_another_destination_replaces_the_one_the_workspaces_own_setup_is_attempting(
    workspace_root: Path, lease_path: Path
) -> None:
    attempting = _begin_an_attempt(workspace_root, lease_path)

    state, is_setup_begun = _request(
        workspace_root,
        lease_path,
        own_storage_request("s3:https://s3.example/bucket", {"AWS_ACCESS_KEY_ID": "k"}),
        now=_NOW + 2.0,
    )

    assert is_setup_begun is True
    assert (state.destination, state.setup.state, state.repository) == (
        BackupDestinationKind.OWN_STORAGE,
        BackupSetupState.INITIALIZING,
        "s3:https://s3.example/bucket",
    )
    assert read_pending_env(
        workspace_root, read_setup_record(workspace_root).request_id
    ) == {"RESTIC_REPOSITORY": "s3:https://s3.example/bucket", "AWS_ACCESS_KEY_ID": "k"}
    assert kept_envs(workspace_root) == [attempting]


def test_a_failed_setup_named_again_is_tried_at_once(
    workspace_root: Path, lease_path: Path
) -> None:
    _request(workspace_root, lease_path, imbue_cloud_request("token-a"))
    failed = failed_record(
        failed_record(
            read_setup_record(workspace_root), "restic init failed", _NOW + 5.0
        ),
        "again",
        _NOW + 70.0,
    )
    write_setup_record(workspace_root, failed)
    assert _state(workspace_root, lease_path).setup.retry_at == _NOW + 70.0 + 120.0

    state, is_setup_begun = _request(
        workspace_root, lease_path, {"destination": "IMBUE_CLOUD"}, now=_NOW + 80.0
    )

    assert is_setup_begun is True
    assert (state.setup.state, state.setup.detail, state.setup.retry_at) == (
        BackupSetupState.INITIALIZING,
        "",
        None,
    )
    after = read_setup_record(workspace_root)
    assert (after.request_id, after.failed_attempts) == (failed.request_id, 0)


def test_new_storage_for_a_setup_under_way_replaces_what_was_offered(
    workspace_root: Path, lease_path: Path
) -> None:
    _request(workspace_root, lease_path, imbue_cloud_request("token-a"))
    first_id = read_setup_record(workspace_root).request_id

    state, is_setup_begun = _request(
        workspace_root, lease_path, imbue_cloud_request("token-b"), now=_NOW + 1.0
    )

    second_id = read_setup_record(workspace_root).request_id
    assert is_setup_begun is True
    assert second_id != first_id
    assert state.setup.state is BackupSetupState.INITIALIZING
    assert read_pending_env(workspace_root, first_id) is None
    pending = read_pending_env(workspace_root, second_id)
    assert pending is not None and pending["RESTIC_REST_PASSWORD"] == "token-b"
