import stat
from pathlib import Path

import pytest
from host_backup.config import RESTIC_ENV_PATH, parse_restic_env_file

from workspace_backup_setup.env_files import (
    PENDING_ENV_RELATIVE_PATH,
    BackupDestinationKind,
    destination_kind_of,
    drop_pending_env,
    install_restic_env,
    keep_restic_env_aside,
    kept_env_for_destination,
    kept_envs,
    kept_password_for_repository,
    read_pending_env,
    render_restic_env,
    repository_without_credentials,
    write_pending_env,
)
from workspace_backup_setup.testing import IMBUE_CLOUD_REPOSITORY, WORKSPACE_ID

_NOW = 1_795_000_000.0
_IMBUE_CLOUD_ENV = {
    "RESTIC_REPOSITORY": IMBUE_CLOUD_REPOSITORY,
    "RESTIC_REST_USERNAME": WORKSPACE_ID,
    "RESTIC_REST_PASSWORD": "token-a",
    "RESTIC_PASSWORD": "repository-password",
}


@pytest.mark.parametrize(
    ("env_values", "workspace_id", "kind"),
    [
        (_IMBUE_CLOUD_ENV, WORKSPACE_ID, BackupDestinationKind.IMBUE_CLOUD),
        (
            _IMBUE_CLOUD_ENV,
            "agent-another-workspace",
            BackupDestinationKind.OWN_STORAGE,
        ),
        (_IMBUE_CLOUD_ENV, "", BackupDestinationKind.IMBUE_CLOUD),
        (
            {
                "RESTIC_REPOSITORY": "rest:https://rest.example/alice/",
                "RESTIC_REST_USERNAME": "alice",
            },
            WORKSPACE_ID,
            BackupDestinationKind.OWN_STORAGE,
        ),
        (
            {
                "RESTIC_REPOSITORY": "rest:https://rest.example/backups/",
                "RESTIC_REST_USERNAME": "alice",
            },
            "",
            BackupDestinationKind.OWN_STORAGE,
        ),
        (
            {"RESTIC_REPOSITORY": "rest:https://rest.example/backups/"},
            WORKSPACE_ID,
            BackupDestinationKind.OWN_STORAGE,
        ),
        (
            {
                "RESTIC_REPOSITORY": "s3:https://s3.example/bucket",
                "RESTIC_REST_USERNAME": WORKSPACE_ID,
            },
            WORKSPACE_ID,
            BackupDestinationKind.OWN_STORAGE,
        ),
        ({}, WORKSPACE_ID, BackupDestinationKind.OWN_STORAGE),
    ],
)
def test_the_destination_is_read_off_a_restic_env(
    env_values: dict[str, str], workspace_id: str, kind: BackupDestinationKind
) -> None:
    assert destination_kind_of(env_values, workspace_id) is kind


@pytest.mark.parametrize(
    ("repository", "shown"),
    [
        (
            "rest:https://alice:hunter2@rest.example/alice/",
            "rest:https://rest.example/alice/",
        ),
        (
            "rest:https://alice:p@ss@rest.example/alice/",
            "rest:https://rest.example/alice/",
        ),
        ("rest:https://rest.example/alice/", "rest:https://rest.example/alice/"),
        ("s3:https://s3.example/bucket", "s3:https://s3.example/bucket"),
        ("/srv/backups/repository", "/srv/backups/repository"),
        ("sftp:user@host:/srv/restic", "sftp:user@host:/srv/restic"),
    ],
)
def test_a_repository_is_shown_with_no_credential_in_its_address(
    repository: str, shown: str
) -> None:
    assert repository_without_credentials(repository) == shown


def test_a_rendered_restic_env_reads_back_and_ends_with_the_password() -> None:
    text = render_restic_env(
        {
            "RESTIC_PASSWORD": "repository-password",
            "AWS_ACCESS_KEY_ID": "key-id",
            "RESTIC_REPOSITORY": "s3:b",
        }
    )

    assert parse_restic_env_file(text) == {
        "RESTIC_REPOSITORY": "s3:b",
        "AWS_ACCESS_KEY_ID": "key-id",
        "RESTIC_PASSWORD": "repository-password",
    }
    assert text.endswith("RESTIC_PASSWORD=repository-password\n")


def test_pending_credentials_are_private_and_read_back_only_for_their_own_request(
    workspace_root: Path,
) -> None:
    write_pending_env(workspace_root, "request-1", _IMBUE_CLOUD_ENV)

    assert (
        stat.S_IMODE((workspace_root / PENDING_ENV_RELATIVE_PATH).stat().st_mode)
        == 0o600
    )
    assert read_pending_env(workspace_root, "request-1") == _IMBUE_CLOUD_ENV
    assert read_pending_env(workspace_root, "request-2") is None


def test_installing_the_same_restic_env_again_leaves_the_file_untouched(
    workspace_root: Path,
) -> None:
    text = render_restic_env(_IMBUE_CLOUD_ENV)
    install_restic_env(workspace_root, text, _NOW)
    path = workspace_root / RESTIC_ENV_PATH
    first = path.stat()

    install_restic_env(workspace_root, text, _NOW + 60.0)

    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (
        first.st_ino,
        first.st_mtime_ns,
    )
    assert stat.S_IMODE(first.st_mode) == 0o600
    assert kept_envs(workspace_root) == []


def test_a_replaced_restic_env_is_kept_beside_the_new_one(workspace_root: Path) -> None:
    install_restic_env(workspace_root, render_restic_env(_IMBUE_CLOUD_ENV), _NOW)
    replacement = {**_IMBUE_CLOUD_ENV, "RESTIC_REST_PASSWORD": "token-b"}

    install_restic_env(workspace_root, render_restic_env(replacement), _NOW + 1.0)
    install_restic_env(workspace_root, render_restic_env(_IMBUE_CLOUD_ENV), _NOW + 1.0)

    assert (
        parse_restic_env_file((workspace_root / RESTIC_ENV_PATH).read_text())
        == _IMBUE_CLOUD_ENV
    )
    # Two copies kept in one second get names of their own, and the newer one is read first.
    assert kept_envs(workspace_root) == [replacement, _IMBUE_CLOUD_ENV]
    kept_names = sorted(
        path.name
        for path in (workspace_root / RESTIC_ENV_PATH).parent.glob("restic.env.2*")
    )
    assert kept_names == [
        "restic.env.20261118T110641Z",
        "restic.env.20261118T110641Z-1",
    ]
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for path in (workspace_root / RESTIC_ENV_PATH).parent.glob("restic.env.2*")
    )


def test_what_was_kept_is_found_by_destination_and_by_repository(
    workspace_root: Path,
) -> None:
    own = {
        "RESTIC_REPOSITORY": "s3:https://s3.example/bucket",
        "RESTIC_PASSWORD": "own-password",
    }
    install_restic_env(workspace_root, render_restic_env(_IMBUE_CLOUD_ENV), _NOW)
    assert keep_restic_env_aside(workspace_root, _NOW + 10.0) is True
    assert keep_restic_env_aside(workspace_root, _NOW + 11.0) is False
    install_restic_env(workspace_root, render_restic_env(own), _NOW + 20.0)

    assert (
        kept_env_for_destination(
            workspace_root, BackupDestinationKind.IMBUE_CLOUD, WORKSPACE_ID
        )
        == _IMBUE_CLOUD_ENV
    )
    assert (
        kept_env_for_destination(
            workspace_root, BackupDestinationKind.OWN_STORAGE, WORKSPACE_ID
        )
        is None
    )
    assert (
        kept_password_for_repository(workspace_root, IMBUE_CLOUD_REPOSITORY)
        == "repository-password"
    )
    assert (
        kept_password_for_repository(workspace_root, "s3:https://s3.example/bucket")
        == "own-password"
    )
    assert (
        kept_password_for_repository(workspace_root, "s3:https://s3.example/another")
        is None
    )


def test_dropped_pending_credentials_are_kept_only_when_they_hold_a_password(
    workspace_root: Path,
) -> None:
    without_password = {
        key: value
        for key, value in _IMBUE_CLOUD_ENV.items()
        if key != "RESTIC_PASSWORD"
    }
    write_pending_env(workspace_root, "request-1", without_password)
    drop_pending_env(workspace_root, _NOW)
    assert not (workspace_root / PENDING_ENV_RELATIVE_PATH).exists()
    assert kept_envs(workspace_root) == []

    write_pending_env(workspace_root, "request-2", _IMBUE_CLOUD_ENV)
    drop_pending_env(workspace_root, _NOW + 1.0)
    write_pending_env(workspace_root, "request-3", _IMBUE_CLOUD_ENV)
    drop_pending_env(workspace_root, _NOW + 2.0)

    assert not (workspace_root / PENDING_ENV_RELATIVE_PATH).exists()
    assert kept_envs(workspace_root) == [_IMBUE_CLOUD_ENV]
