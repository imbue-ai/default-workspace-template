import json
import os
import stat
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest
from host_backup.config import RESTIC_ENV_PATH

from workspace_backup_setup.errors import (
    BackupSetupRunningError,
    InvalidBackupCredentialsError,
)
from workspace_backup_setup.setup import (
    PENDING_ENV_RELATIVE_PATH,
    SETUP_STATE_RELATIVE_PATH,
    BackupSetupState,
    BackupSetupStatus,
    begin_setup,
    read_setup_status,
    remove_credentials,
)
from workspace_backup_setup.testing import (
    FAKE_RESTIC_FAILS,
    FAKE_RESTIC_FINDS_A_REPOSITORY,
    FAKE_RESTIC_FINDS_ANOTHER_PASSWORDS_REPOSITORY,
    fake_restic_calls,
    restic_env_text,
    set_fake_restic_mode,
)

_PROC_ROOT = Path("/proc")
_REPOSITORY = "rest:https://backups.example/workspace-5f3c"
_PASSWORD = "correct-horse-battery-staple-91f2"
_ENV_TEXT = restic_env_text(_REPOSITORY, _PASSWORD)
_STARTED_AT = 1_795_000_000.0


def _run_in_place(work: Callable[[], None]) -> None:
    work()


def _set_up(workspace_root: Path, env_text: str, run_in_background: Callable[[Callable[[], None]], None]) -> BackupSetupStatus:
    return begin_setup(
        workspace_root, env_text, proc_root=_PROC_ROOT, run_in_background=run_in_background, clock=lambda: _STARTED_AT
    )


def _moved_aside_copies(workspace_root: Path) -> list[Path]:
    return sorted((workspace_root / RESTIC_ENV_PATH).parent.glob(f"{RESTIC_ENV_PATH.name}.2*"))


def test_no_credentials_ever_sent_reads_as_none(workspace_root: Path) -> None:
    assert read_setup_status(workspace_root, _PROC_ROOT) == BackupSetupStatus(state=BackupSetupState.NONE)


def test_credentials_initialise_the_repository_and_only_then_write_restic_env(
    workspace_root: Path, fake_restic_control: Path
) -> None:
    deferred: list[Callable[[], None]] = []

    answered = _set_up(workspace_root, _ENV_TEXT, deferred.append)

    assert answered.state is BackupSetupState.INITIALIZING
    assert not (workspace_root / RESTIC_ENV_PATH).exists()
    pending = workspace_root / PENDING_ENV_RELATIVE_PATH
    assert pending.read_text() == _ENV_TEXT
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    assert read_setup_status(workspace_root, _PROC_ROOT).state is BackupSetupState.INITIALIZING

    deferred[0]()

    status = read_setup_status(workspace_root, _PROC_ROOT)
    assert status.state is BackupSetupState.READY
    assert status.finished_at == _STARTED_AT
    restic_env = workspace_root / RESTIC_ENV_PATH
    assert restic_env.read_text() == _ENV_TEXT
    assert stat.S_IMODE(restic_env.stat().st_mode) == 0o600
    assert not pending.exists()
    assert fake_restic_calls(fake_restic_control) == [f"{_REPOSITORY}|init"]


def test_the_same_credentials_already_in_place_and_ready_change_nothing(
    workspace_root: Path, fake_restic_control: Path
) -> None:
    _set_up(workspace_root, _ENV_TEXT, _run_in_place)
    restic_env = workspace_root / RESTIC_ENV_PATH
    os.utime(restic_env, ns=(1_000_000_000, 1_000_000_000))

    answered = _set_up(workspace_root, _ENV_TEXT, _run_in_place)

    assert answered.state is BackupSetupState.READY
    assert restic_env.stat().st_mtime_ns == 1_000_000_000
    assert fake_restic_calls(fake_restic_control) == [f"{_REPOSITORY}|init"]
    assert _moved_aside_copies(workspace_root) == []


def test_different_credentials_move_the_old_file_aside(workspace_root: Path, fake_restic_control: Path) -> None:
    _set_up(workspace_root, _ENV_TEXT, _run_in_place)
    other_env_text = restic_env_text("rest:https://backups.example/another", "another-password-77aa")

    _set_up(workspace_root, other_env_text, _run_in_place)

    assert (workspace_root / RESTIC_ENV_PATH).read_text() == other_env_text
    moved = _moved_aside_copies(workspace_root)
    assert [path.read_text() for path in moved] == [_ENV_TEXT]
    assert moved[0].name == "restic.env." + datetime.fromtimestamp(_STARTED_AT, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def test_a_file_written_before_the_routes_with_the_same_contents_is_left_alone(
    workspace_root: Path, fake_restic_control: Path
) -> None:
    restic_env = workspace_root / RESTIC_ENV_PATH
    restic_env.parent.mkdir(parents=True)
    restic_env.write_text(_ENV_TEXT)
    os.utime(restic_env, ns=(1_000_000_000, 1_000_000_000))
    set_fake_restic_mode(fake_restic_control, FAKE_RESTIC_FINDS_A_REPOSITORY)

    answered = _set_up(workspace_root, _ENV_TEXT, _run_in_place)

    assert answered.state is BackupSetupState.INITIALIZING
    assert read_setup_status(workspace_root, _PROC_ROOT).state is BackupSetupState.READY
    assert restic_env.stat().st_mtime_ns == 1_000_000_000
    assert _moved_aside_copies(workspace_root) == []


def test_a_repository_that_already_exists_is_ready_when_the_password_opens_it(
    workspace_root: Path, fake_restic_control: Path
) -> None:
    set_fake_restic_mode(fake_restic_control, FAKE_RESTIC_FINDS_A_REPOSITORY)

    _set_up(workspace_root, _ENV_TEXT, _run_in_place)

    assert read_setup_status(workspace_root, _PROC_ROOT).state is BackupSetupState.READY
    assert fake_restic_calls(fake_restic_control) == [f"{_REPOSITORY}|init", f"{_REPOSITORY}|--no-lock cat config"]
    assert (workspace_root / RESTIC_ENV_PATH).read_text() == _ENV_TEXT


def test_a_repository_another_password_initialised_fails_and_writes_nothing(
    workspace_root: Path, fake_restic_control: Path
) -> None:
    set_fake_restic_mode(fake_restic_control, FAKE_RESTIC_FINDS_ANOTHER_PASSWORDS_REPOSITORY)

    _set_up(workspace_root, _ENV_TEXT, _run_in_place)

    status = read_setup_status(workspace_root, _PROC_ROOT)
    assert status.state is BackupSetupState.FAILED
    assert "different password" in status.detail
    assert not (workspace_root / RESTIC_ENV_PATH).exists()
    assert not (workspace_root / PENDING_ENV_RELATIVE_PATH).exists()


def test_a_failed_initialisation_leaves_the_file_that_was_there_and_reports_no_credential(
    workspace_root: Path, fake_restic_control: Path
) -> None:
    _set_up(workspace_root, _ENV_TEXT, _run_in_place)
    set_fake_restic_mode(fake_restic_control, FAKE_RESTIC_FAILS)
    other_password = "another-password-77aa"
    other_repository = "rest:https://user:secret-in-url@backups.example/another"

    _set_up(workspace_root, restic_env_text(other_repository, other_password), _run_in_place)

    status = read_setup_status(workspace_root, _PROC_ROOT)
    assert status.state is BackupSetupState.FAILED
    assert status.detail.startswith("restic init failed (exit 1): Fatal: could not reach [redacted]")
    assert other_password not in status.detail
    assert "secret-in-url" not in status.detail
    assert (workspace_root / RESTIC_ENV_PATH).read_text() == _ENV_TEXT
    assert not (workspace_root / PENDING_ENV_RELATIVE_PATH).exists()
    state_text = (workspace_root / SETUP_STATE_RELATIVE_PATH).read_text()
    assert other_password not in state_text
    assert "secret-in-url" not in state_text


def test_an_initialisation_that_raises_reads_as_failed_and_holds_no_later_setup_back(
    workspace_root: Path, fake_restic_control: Path
) -> None:
    # A value holding a NUL cannot be put in a process's environment, so restic is never started.
    unstartable_env_text = restic_env_text(_REPOSITORY, "half\x00password")

    with pytest.raises(ValueError):
        _set_up(workspace_root, unstartable_env_text, _run_in_place)

    status = read_setup_status(workspace_root, _PROC_ROOT)
    assert status.state is BackupSetupState.FAILED
    assert "half" not in status.detail
    assert not (workspace_root / PENDING_ENV_RELATIVE_PATH).exists()
    assert not (workspace_root / RESTIC_ENV_PATH).exists()
    _set_up(workspace_root, _ENV_TEXT, _run_in_place)
    assert read_setup_status(workspace_root, _PROC_ROOT).state is BackupSetupState.READY


def test_a_second_request_while_one_initialisation_runs_is_refused(
    workspace_root: Path, fake_restic_control: Path
) -> None:
    deferred: list[Callable[[], None]] = []
    _set_up(workspace_root, _ENV_TEXT, deferred.append)

    with pytest.raises(BackupSetupRunningError):
        _set_up(workspace_root, restic_env_text("rest:https://backups.example/other", "other-password"), deferred.append)

    assert len(deferred) == 1
    assert (workspace_root / PENDING_ENV_RELATIVE_PATH).read_text() == _ENV_TEXT


def test_a_state_left_initializing_by_a_process_that_is_gone_reads_as_failed_and_can_be_sent_again(
    workspace_root: Path, fake_restic_control: Path, exited_pid: int
) -> None:
    state_path = workspace_root / SETUP_STATE_RELATIVE_PATH
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"schema": 1, "state": "INITIALIZING", "started_at": _STARTED_AT, "pid": exited_pid})
    )

    status = read_setup_status(workspace_root, _PROC_ROOT)

    assert status.state is BackupSetupState.FAILED
    assert "is gone" in status.detail
    _set_up(workspace_root, _ENV_TEXT, _run_in_place)
    assert read_setup_status(workspace_root, _PROC_ROOT).state is BackupSetupState.READY


@pytest.mark.parametrize(
    ("env_text", "missing"),
    [
        ("RESTIC_REPOSITORY=rest:https://backups.example/x\n", "RESTIC_PASSWORD"),
        ("RESTIC_PASSWORD=secret\n", "RESTIC_REPOSITORY"),
        ("", "RESTIC_REPOSITORY and RESTIC_PASSWORD"),
    ],
)
def test_text_that_does_not_set_the_repository_and_its_password_is_refused(
    workspace_root: Path, fake_restic_control: Path, env_text: str, missing: str
) -> None:
    with pytest.raises(InvalidBackupCredentialsError, match=missing):
        _set_up(workspace_root, env_text, _run_in_place)

    assert fake_restic_calls(fake_restic_control) == []
    assert not (workspace_root / SETUP_STATE_RELATIVE_PATH).exists()


def test_removing_the_credentials_moves_the_file_aside_and_starts_the_state_over(
    workspace_root: Path, fake_restic_control: Path
) -> None:
    _set_up(workspace_root, _ENV_TEXT, _run_in_place)
    removed_at = datetime(2026, 11, 24, 12, 30, 5, tzinfo=timezone.utc)

    assert remove_credentials(workspace_root, removed_at, proc_root=_PROC_ROOT) is True

    assert not (workspace_root / RESTIC_ENV_PATH).exists()
    assert (workspace_root / RESTIC_ENV_PATH).with_name("restic.env.20261124T123005Z").read_text() == _ENV_TEXT
    assert read_setup_status(workspace_root, _PROC_ROOT).state is BackupSetupState.NONE
    assert remove_credentials(workspace_root, removed_at, proc_root=_PROC_ROOT) is False


def test_the_credentials_cannot_be_removed_while_a_repository_is_being_initialised(
    workspace_root: Path, fake_restic_control: Path
) -> None:
    deferred: list[Callable[[], None]] = []
    _set_up(workspace_root, _ENV_TEXT, deferred.append)

    with pytest.raises(BackupSetupRunningError):
        remove_credentials(workspace_root, datetime.now(timezone.utc), proc_root=_PROC_ROOT)
