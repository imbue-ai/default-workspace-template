import shutil
import stat
from pathlib import Path

import pytest
from host_backup.config import RESTIC_ENV_PATH, parse_restic_env_file
from host_backup.restic import init_repo, run_restic
from workspace_bare_scripts.backup_lease import read_lease, release_lease, take_lease

from workspace_backup_setup.destination import request_backup_destination
from workspace_backup_setup.env_files import (
    PENDING_ENV_RELATIVE_PATH,
    BackupDestinationKind,
    env_digest,
    kept_envs,
    read_pending_env,
)
from workspace_backup_setup.program import (
    CREDENTIALS_CHECK_INTERVAL_SECONDS,
    BackupSetupProgram,
)
from workspace_backup_setup.state import (
    BackupSetupState,
    BackupState,
    read_backup_credentials,
    read_backup_state,
    read_setup_record,
)
from workspace_backup_setup.testing import (
    FAKE_RESTIC_FAILS,
    FAKE_RESTIC_FINDS_A_REPOSITORY,
    IMBUE_CLOUD_REPOSITORY,
    WORKSPACE_ID,
    SettableClock,
    fake_restic_calls,
    imbue_cloud_request,
    own_storage_request,
    refuse_fake_restic_token,
    set_fake_restic_mode,
)

_NOW = 1_795_000_000.0
_STORAGE_REFUSED = (
    "The storage does not accept the credentials this workspace holds for it."
)
_WRONG_PASSWORD = "The repository is already initialized with a password this workspace does not hold."

needs_restic = pytest.mark.skipif(
    shutil.which("restic") is None,
    reason="restic binary not on PATH (install via apt-get install restic to enable)",
)
# A real restic derives a repository's key on purpose slowly, several times in one of these tests.
_RESTIC_TEST_TIMEOUT_SECONDS = 60


class _Passwords:
    """Makes the passwords a test expects, and counts them."""

    def __init__(self) -> None:
        self.made: list[str] = []

    def __call__(self) -> str:
        self.made.append(f"made-password-{len(self.made) + 1}")
        return self.made[-1]


class _Workspace:
    """A workspace, its lease, a clock the test moves, and the programs it starts."""

    def __init__(self, root: Path, lease_path: Path) -> None:
        self.root = root
        self.lease_path = lease_path
        self.clock = SettableClock(_NOW)
        self.passwords = _Passwords()

    def start_program(self) -> BackupSetupProgram:
        return BackupSetupProgram(
            self.root,
            lease_path=self.lease_path,
            clock=self.clock,
            workspace_id=WORKSPACE_ID,
            make_password=self.passwords,
        )

    def request(self, body: dict[str, object]) -> BackupState:
        return request_backup_destination(
            self.root,
            body,
            lease_path=self.lease_path,
            now=self.clock(),
            workspace_id=WORKSPACE_ID,
        ).state

    def state(self) -> BackupState:
        return read_backup_state(
            self.root,
            lease_path=self.lease_path,
            now=self.clock(),
            workspace_id=WORKSPACE_ID,
        )

    @property
    def restic_env(self) -> Path:
        return self.root / RESTIC_ENV_PATH

    def installed(self) -> dict[str, str]:
        return parse_restic_env_file(self.restic_env.read_text())

    def kept_aside(self) -> list[dict[str, str]]:
        return kept_envs(self.root)


@pytest.fixture
def workspace(workspace_root: Path, lease_path: Path) -> _Workspace:
    return _Workspace(workspace_root, lease_path)


def _set_up_imbue_cloud(workspace: _Workspace, token: str) -> BackupSetupProgram:
    """A workspace backing up to Imbue Cloud under ``token``, and the program that set it up."""
    program = workspace.start_program()
    workspace.request(imbue_cloud_request(token))
    program.run_pass()
    assert workspace.state().setup.state is BackupSetupState.READY
    return program


@needs_restic
@pytest.mark.timeout(_RESTIC_TEST_TIMEOUT_SECONDS)
def test_a_repository_that_is_not_there_is_initialised_with_a_password_the_workspace_makes(
    workspace: _Workspace, tmp_path: Path
) -> None:
    repository = str(tmp_path / "repository")
    program = workspace.start_program()

    asked = workspace.request(
        own_storage_request(repository, {"AWS_ACCESS_KEY_ID": "key-id"})
    )

    assert (asked.destination, asked.setup.state) == (
        BackupDestinationKind.OWN_STORAGE,
        BackupSetupState.INITIALIZING,
    )
    assert not workspace.restic_env.exists()

    program.run_pass()

    state = workspace.state()
    assert (state.setup.state, state.setup.detail, state.is_configured) == (
        BackupSetupState.READY,
        "",
        True,
    )
    assert (state.setup.started_at, state.setup.finished_at, state.setup.retry_at) == (
        _NOW,
        _NOW,
        None,
    )
    assert state.repository == repository
    assert state.lease is None
    assert workspace.installed() == {
        "RESTIC_REPOSITORY": repository,
        "AWS_ACCESS_KEY_ID": "key-id",
        "RESTIC_PASSWORD": "made-password-1",
    }
    assert stat.S_IMODE(workspace.restic_env.stat().st_mode) == 0o600
    assert not (workspace.root / PENDING_ENV_RELATIVE_PATH).exists()
    assert (
        run_restic(("cat", "config"), env_overrides=workspace.installed()).returncode
        == 0
    )
    credentials = read_backup_credentials(workspace.root)
    assert credentials.env_text == workspace.restic_env.read_text()
    assert (
        credentials.digest
        == state.credentials_digest
        == env_digest(credentials.env_text)
    )


@needs_restic
@pytest.mark.timeout(_RESTIC_TEST_TIMEOUT_SECONDS)
def test_a_repository_named_again_is_opened_with_the_password_kept_for_it(
    workspace: _Workspace, tmp_path: Path
) -> None:
    repository = str(tmp_path / "repository")
    program = workspace.start_program()
    workspace.request(own_storage_request(repository, {}))
    program.run_pass()
    first_text = workspace.restic_env.read_text()

    off = workspace.request({"destination": "NONE"})
    assert (off.destination, off.setup.state, off.is_configured) == (
        BackupDestinationKind.NONE,
        BackupSetupState.NONE,
        True,
    )
    program.run_pass()

    state = workspace.state()
    assert (
        state.is_configured,
        state.credentials_digest,
        state.repository,
        state.lease,
    ) == (False, "", "", None)
    assert workspace.kept_aside() == [parse_restic_env_file(first_text)]

    again = workspace.request({"destination": "OWN_STORAGE"})
    assert (again.setup.state, again.repository, again.is_configured) == (
        BackupSetupState.INITIALIZING,
        repository,
        False,
    )
    program.run_pass()

    assert workspace.state().setup.state is BackupSetupState.READY
    assert workspace.installed()["RESTIC_PASSWORD"] == "made-password-1"
    assert workspace.passwords.made == ["made-password-1"]
    assert (
        run_restic(("cat", "config"), env_overrides=workspace.installed()).returncode
        == 0
    )


@needs_restic
@pytest.mark.timeout(_RESTIC_TEST_TIMEOUT_SECONDS)
def test_a_kept_password_that_does_not_open_the_repository_fails_and_installs_nothing(
    workspace: _Workspace, tmp_path: Path
) -> None:
    repository = str(tmp_path / "repository")
    assert (
        init_repo(
            {"RESTIC_REPOSITORY": repository, "RESTIC_PASSWORD": "the-only-key"}
        ).returncode
        == 0
    )
    workspace.restic_env.parent.mkdir(parents=True)
    (workspace.restic_env.parent / "restic.env.20260301T101500Z").write_text(
        f"RESTIC_REPOSITORY={repository}\nRESTIC_PASSWORD=not-the-key\n"
    )
    program = workspace.start_program()

    workspace.request({"destination": "OWN_STORAGE"})
    program.run_pass()

    state = workspace.state()
    assert (state.setup.state, state.setup.detail) == (
        BackupSetupState.FAILED,
        _WRONG_PASSWORD,
    )
    assert state.setup.retry_at == _NOW + 60.0
    assert (state.is_configured, state.lease) == (False, None)
    assert workspace.passwords.made == []
    pending = read_pending_env(
        workspace.root, read_setup_record(workspace.root).request_id
    )
    assert pending is not None and pending["RESTIC_PASSWORD"] == "not-the-key"


def test_a_failed_setup_is_tried_again_after_a_minute_then_twice_as_long_up_to_an_hour(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    set_fake_restic_mode(fake_restic_control, FAKE_RESTIC_FAILS)
    program = workspace.start_program()
    workspace.request(imbue_cloud_request("token-a"))

    delays: list[float] = []
    for attempt in range(1, 9):
        program.run_pass()
        state = workspace.state()
        assert state.setup.state is BackupSetupState.FAILED
        assert len(fake_restic_calls(fake_restic_control)) == attempt
        assert state.setup.retry_at is not None
        delays.append(state.setup.retry_at - workspace.clock.now)
        workspace.clock.now = state.setup.retry_at - 1.0
        program.run_pass()
        assert len(fake_restic_calls(fake_restic_control)) == attempt
        workspace.clock.now = state.setup.retry_at

    assert delays == [60.0, 120.0, 240.0, 480.0, 960.0, 1920.0, 3600.0, 3600.0]
    detail = workspace.state().setup.detail
    assert detail.startswith(
        "restic init failed (exit 1): Fatal: could not reach [redacted] with password [redacted]"
    )
    assert not workspace.restic_env.exists()
    assert workspace.passwords.made == ["made-password-1"]

    set_fake_restic_mode(fake_restic_control, FAKE_RESTIC_FINDS_A_REPOSITORY)
    program.run_pass()

    state = workspace.state()
    assert (state.setup.state, state.setup.detail, state.setup.retry_at) == (
        BackupSetupState.READY,
        "",
        None,
    )
    assert workspace.installed()["RESTIC_PASSWORD"] == "made-password-1"
    assert read_setup_record(workspace.root).failed_attempts == 0


class _ProgramStopped(BaseException):
    """Stands in for the program being stopped in the middle of a setup."""


def test_a_setup_the_program_was_stopped_in_is_taken_up_when_it_starts(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    def _stop_the_program() -> str:
        raise _ProgramStopped()

    stopped = BackupSetupProgram(
        workspace.root,
        lease_path=workspace.lease_path,
        clock=workspace.clock,
        workspace_id=WORKSPACE_ID,
        make_password=_stop_the_program,
    )
    workspace.request(imbue_cloud_request("token-a"))
    with pytest.raises(_ProgramStopped):
        stopped.run_pass()
    assert workspace.state().setup.state is BackupSetupState.INITIALIZING
    assert fake_restic_calls(fake_restic_control) == []
    # A program that is killed gives nothing back.
    take_lease(workspace.lease_path, "setup", workspace.clock())

    workspace.clock.advance(5.0)
    workspace.start_program().run_pass()

    state = workspace.state()
    assert (state.setup.state, state.is_configured, state.lease) == (
        BackupSetupState.READY,
        True,
        None,
    )
    assert fake_restic_calls(fake_restic_control) == [f"{IMBUE_CLOUD_REPOSITORY}|init"]


def test_a_destination_named_during_an_attempt_ends_the_attempt_and_is_set_up_next(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    def _name_other_storage_then_make_a_password() -> str:
        workspace.request(imbue_cloud_request("token-b"))
        return workspace.passwords()

    program = BackupSetupProgram(
        workspace.root,
        lease_path=workspace.lease_path,
        clock=workspace.clock,
        workspace_id=WORKSPACE_ID,
        make_password=_name_other_storage_then_make_a_password,
    )
    workspace.request(imbue_cloud_request("token-a"))

    program.run_pass()

    # The attempt at token-a stops short of the repository, and leaves the storage named since where it is.
    superseding = workspace.state()
    assert (superseding.setup.state, superseding.is_configured, superseding.lease) == (
        BackupSetupState.INITIALIZING,
        False,
        None,
    )
    assert fake_restic_calls(fake_restic_control) == []
    assert read_pending_env(
        workspace.root, read_setup_record(workspace.root).request_id
    ) == {
        "RESTIC_REPOSITORY": IMBUE_CLOUD_REPOSITORY,
        "RESTIC_REST_USERNAME": WORKSPACE_ID,
        "RESTIC_REST_PASSWORD": "token-b",
    }

    program.run_pass()

    assert workspace.state().setup.state is BackupSetupState.READY
    assert workspace.installed()["RESTIC_REST_PASSWORD"] == "token-b"
    assert workspace.installed()["RESTIC_PASSWORD"] == "made-password-2"


def test_backups_turned_off_during_an_attempt_end_it_with_nothing_installed(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    def _turn_backups_off_then_make_a_password() -> str:
        workspace.request({"destination": "NONE"})
        return workspace.passwords()

    program = BackupSetupProgram(
        workspace.root,
        lease_path=workspace.lease_path,
        clock=workspace.clock,
        workspace_id=WORKSPACE_ID,
        make_password=_turn_backups_off_then_make_a_password,
    )
    workspace.request(imbue_cloud_request("token-a"))

    program.run_pass()
    program.run_pass()

    state = workspace.state()
    assert (state.destination, state.setup.state, state.is_configured, state.lease) == (
        BackupDestinationKind.NONE,
        BackupSetupState.NONE,
        False,
        None,
    )
    assert fake_restic_calls(fake_restic_control) == []
    assert not (workspace.root / PENDING_ENV_RELATIVE_PATH).exists()


def test_a_failed_setup_is_tried_as_soon_as_the_program_starts(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    set_fake_restic_mode(fake_restic_control, FAKE_RESTIC_FAILS)
    workspace.request(imbue_cloud_request("token-a"))
    workspace.start_program().run_pass()
    assert workspace.state().setup.state is BackupSetupState.FAILED
    made_before = list(workspace.passwords.made)

    set_fake_restic_mode(fake_restic_control, FAKE_RESTIC_FINDS_A_REPOSITORY)
    workspace.clock.advance(1.0)
    workspace.start_program().run_pass()

    assert workspace.state().setup.state is BackupSetupState.READY
    assert workspace.passwords.made == made_before == ["made-password-1"]


def test_the_credentials_in_use_are_checked_when_the_program_starts_and_once_an_hour(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    _set_up_imbue_cloud(workspace, "token-a")
    calls_after_setup = len(fake_restic_calls(fake_restic_control))

    program = workspace.start_program()
    program.run_pass()
    program.run_pass()
    assert fake_restic_calls(fake_restic_control)[calls_after_setup:] == [
        f"{IMBUE_CLOUD_REPOSITORY}|--no-lock cat config"
    ]

    workspace.clock.advance(CREDENTIALS_CHECK_INTERVAL_SECONDS - 1.0)
    program.run_pass()
    assert len(fake_restic_calls(fake_restic_control)) == calls_after_setup + 1
    workspace.clock.advance(1.0)
    program.run_pass()
    assert len(fake_restic_calls(fake_restic_control)) == calls_after_setup + 2
    assert workspace.state().setup.state is BackupSetupState.READY


def test_a_storage_that_refuses_the_credentials_in_use_leaves_the_setup_awaiting_storage_and_the_file_in_place(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    program = _set_up_imbue_cloud(workspace, "token-a")
    installed_text = workspace.restic_env.read_text()

    refuse_fake_restic_token(fake_restic_control, "token-a")
    workspace.clock.advance(CREDENTIALS_CHECK_INTERVAL_SECONDS)
    program.run_pass()

    state = workspace.state()
    assert (state.destination, state.setup.state, state.setup.detail) == (
        BackupDestinationKind.IMBUE_CLOUD,
        BackupSetupState.AWAITING_STORAGE,
        _STORAGE_REFUSED,
    )
    assert (state.is_configured, state.credentials_digest) == (
        True,
        env_digest(installed_text),
    )
    assert workspace.restic_env.read_text() == installed_text

    (fake_restic_control / "refused-tokens").unlink()
    workspace.clock.advance(CREDENTIALS_CHECK_INTERVAL_SECONDS)
    program.run_pass()
    assert workspace.state().setup.state is BackupSetupState.READY


def test_a_check_that_fails_for_any_other_reason_changes_nothing(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    program = _set_up_imbue_cloud(workspace, "token-a")
    before = workspace.state()
    calls_before = len(fake_restic_calls(fake_restic_control))

    set_fake_restic_mode(fake_restic_control, FAKE_RESTIC_FAILS)
    workspace.clock.advance(CREDENTIALS_CHECK_INTERVAL_SECONDS)
    program.run_pass()

    assert len(fake_restic_calls(fake_restic_control)) == calls_before + 1
    assert workspace.state() == before


def test_storage_offered_for_the_repository_in_use_is_let_go_while_the_storage_accepts_what_is_held(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    program = _set_up_imbue_cloud(workspace, "token-a")
    installed_text = workspace.restic_env.read_text()

    refuse_fake_restic_token(fake_restic_control, "token-b")
    asked = workspace.request(imbue_cloud_request("token-b"))
    assert asked.setup.state is BackupSetupState.INITIALIZING
    assert asked.credentials_digest == env_digest(installed_text)
    program.run_pass()

    state = workspace.state()
    assert (state.setup.state, state.credentials_digest) == (
        BackupSetupState.READY,
        env_digest(installed_text),
    )
    assert workspace.restic_env.read_text() == installed_text
    assert not (workspace.root / PENDING_ENV_RELATIVE_PATH).exists()
    assert workspace.kept_aside() == []


def test_storage_offered_for_the_repository_in_use_is_taken_once_the_storage_refuses_what_is_held(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    program = _set_up_imbue_cloud(workspace, "token-a")
    held = workspace.installed()

    refuse_fake_restic_token(fake_restic_control, "token-a")
    set_fake_restic_mode(fake_restic_control, FAKE_RESTIC_FINDS_A_REPOSITORY)
    workspace.request(imbue_cloud_request("token-b"))
    program.run_pass()

    state = workspace.state()
    assert state.setup.state is BackupSetupState.READY
    assert workspace.installed() == {**held, "RESTIC_REST_PASSWORD": "token-b"}
    assert workspace.passwords.made == ["made-password-1"]
    assert workspace.kept_aside() == [held]


def test_storage_the_storage_refuses_leaves_the_setup_awaiting_storage_until_it_is_accepted(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    refuse_fake_restic_token(fake_restic_control, "token-a")
    program = workspace.start_program()
    workspace.request(imbue_cloud_request("token-a"))

    program.run_pass()
    program.run_pass()

    state = workspace.state()
    assert (state.setup.state, state.setup.detail, state.setup.retry_at) == (
        BackupSetupState.AWAITING_STORAGE,
        _STORAGE_REFUSED,
        None,
    )
    assert (state.is_configured, state.lease) == (False, None)
    assert len(fake_restic_calls(fake_restic_control)) == 1

    (fake_restic_control / "refused-tokens").unlink()
    workspace.clock.advance(CREDENTIALS_CHECK_INTERVAL_SECONDS)
    program.run_pass()
    assert workspace.state().setup.state is BackupSetupState.READY
    assert workspace.installed()["RESTIC_REST_PASSWORD"] == "token-a"


def test_none_moves_restic_env_aside_and_a_destination_named_again_is_installed_from_what_was_kept(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    program = _set_up_imbue_cloud(workspace, "token-a")
    held = workspace.installed()

    workspace.request({"destination": "NONE"})
    program.run_pass()

    off = workspace.state()
    assert (off.destination, off.setup.state, off.is_configured, off.lease) == (
        BackupDestinationKind.NONE,
        BackupSetupState.NONE,
        False,
        None,
    )
    assert workspace.kept_aside() == [held]

    set_fake_restic_mode(fake_restic_control, FAKE_RESTIC_FINDS_A_REPOSITORY)
    workspace.request({"destination": "IMBUE_CLOUD"})
    program.run_pass()

    assert workspace.state().setup.state is BackupSetupState.READY
    assert workspace.installed() == held
    assert workspace.passwords.made == ["made-password-1"]


def test_the_previous_destination_stays_in_use_until_the_new_one_is_ready_and_is_kept_then(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    program = _set_up_imbue_cloud(workspace, "token-a")
    held = workspace.installed()
    held_text = workspace.restic_env.read_text()

    set_fake_restic_mode(fake_restic_control, FAKE_RESTIC_FAILS)
    workspace.request(
        own_storage_request(
            "s3:https://s3.example/bucket", {"AWS_ACCESS_KEY_ID": "key-id"}
        )
    )
    program.run_pass()

    failing = workspace.state()
    assert (failing.destination, failing.setup.state) == (
        BackupDestinationKind.OWN_STORAGE,
        BackupSetupState.FAILED,
    )
    assert (failing.repository, failing.credentials_digest) == (
        "s3:https://s3.example/bucket",
        env_digest(held_text),
    )
    assert workspace.restic_env.read_text() == held_text

    back = workspace.request({"destination": "IMBUE_CLOUD"})
    assert (back.destination, back.setup.state, back.repository) == (
        BackupDestinationKind.IMBUE_CLOUD,
        BackupSetupState.READY,
        IMBUE_CLOUD_REPOSITORY,
    )
    assert workspace.restic_env.read_text() == held_text

    set_fake_restic_mode(fake_restic_control, "ok")
    workspace.request(
        own_storage_request(
            "s3:https://s3.example/bucket", {"AWS_ACCESS_KEY_ID": "key-id"}
        )
    )
    program.run_pass()

    changed = workspace.state()
    assert (changed.destination, changed.setup.state) == (
        BackupDestinationKind.OWN_STORAGE,
        BackupSetupState.READY,
    )
    assert workspace.installed()["RESTIC_REPOSITORY"] == "s3:https://s3.example/bucket"
    assert held in workspace.kept_aside()


def test_a_restic_env_another_writer_put_in_place_is_taken_as_the_destination(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    set_fake_restic_mode(fake_restic_control, FAKE_RESTIC_FAILS)
    program = workspace.start_program()
    workspace.request(
        own_storage_request(
            "s3:https://s3.example/bucket", {"AWS_ACCESS_KEY_ID": "key-id"}
        )
    )
    program.run_pass()
    assert workspace.state().setup.state is BackupSetupState.FAILED
    calls_before = len(fake_restic_calls(fake_restic_control))

    desktop_text = (
        "# Managed by Imbue Studio.\n"
        f"RESTIC_REPOSITORY={IMBUE_CLOUD_REPOSITORY}\nRESTIC_REST_USERNAME={WORKSPACE_ID}\n"
        "RESTIC_REST_PASSWORD=desktop-token\nRESTIC_PASSWORD=desktop-password\n"
    )
    workspace.restic_env.write_text(desktop_text)
    set_fake_restic_mode(fake_restic_control, FAKE_RESTIC_FINDS_A_REPOSITORY)

    read_before_the_program_ran = workspace.state()
    workspace.clock.advance(3600.0)
    program.run_pass()

    state = workspace.state()
    assert state == read_before_the_program_ran
    assert (
        state.destination,
        state.setup.state,
        state.setup.detail,
        state.setup.retry_at,
    ) == (
        BackupDestinationKind.IMBUE_CLOUD,
        BackupSetupState.READY,
        "",
        None,
    )
    assert (state.repository, state.credentials_digest) == (
        IMBUE_CLOUD_REPOSITORY,
        env_digest(desktop_text),
    )
    assert workspace.restic_env.read_text() == desktop_text
    assert not (workspace.root / PENDING_ENV_RELATIVE_PATH).exists()
    assert [
        call
        for call in fake_restic_calls(fake_restic_control)[calls_before:]
        if call.endswith("|init")
    ] == []
    # The setup that was dropped had made a password, which is kept in case the repository holds it.
    assert [kept["RESTIC_PASSWORD"] for kept in workspace.kept_aside()] == [
        "made-password-1"
    ]


def test_a_restic_env_for_another_storage_reads_as_the_users_own(
    workspace: _Workspace,
) -> None:
    workspace.restic_env.parent.mkdir(parents=True)
    workspace.restic_env.write_text(
        "RESTIC_REPOSITORY=rest:https://alice:hunter2@rest.example/alice/\nRESTIC_REST_USERNAME=alice\n"
        "RESTIC_PASSWORD=desktop-password\n"
    )

    state = workspace.state()

    assert (state.destination, state.setup.state) == (
        BackupDestinationKind.OWN_STORAGE,
        BackupSetupState.READY,
    )
    assert state.repository == "rest:https://rest.example/alice/"


def test_a_restic_env_another_writer_moved_aside_is_backups_turned_off(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    program = _set_up_imbue_cloud(workspace, "token-a")
    calls_before = len(fake_restic_calls(fake_restic_control))

    workspace.restic_env.rename(
        workspace.restic_env.with_name("restic.env.20261126T120000Z")
    )
    workspace.clock.advance(CREDENTIALS_CHECK_INTERVAL_SECONDS)
    program.run_pass()

    state = workspace.state()
    assert (
        state.destination,
        state.setup.state,
        state.is_configured,
        state.repository,
    ) == (
        BackupDestinationKind.NONE,
        BackupSetupState.NONE,
        False,
        "",
    )
    assert len(fake_restic_calls(fake_restic_control)) == calls_before
    assert read_setup_record(workspace.root).destination is BackupDestinationKind.NONE


def test_the_setup_waits_while_someone_else_holds_the_backup_lease(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    program = workspace.start_program()
    workspace.request(imbue_cloud_request("token-a"))
    taken = take_lease(workspace.lease_path, "restore", workspace.clock())

    program.run_pass()

    state = workspace.state()
    assert state.setup.state is BackupSetupState.INITIALIZING
    assert state.lease is not None and (state.lease.holder, state.lease.expires_at) == (
        "restore",
        taken.expires_at,
    )
    assert fake_restic_calls(fake_restic_control) == []
    assert read_lease(workspace.lease_path, workspace.clock()) == taken

    release_lease(workspace.lease_path, "restore", workspace.clock())
    program.run_pass()
    assert workspace.state().setup.state is BackupSetupState.READY
    assert workspace.state().lease is None


def test_backups_are_not_turned_off_under_someone_elses_backup_lease(
    workspace: _Workspace, fake_restic_control: Path
) -> None:
    program = _set_up_imbue_cloud(workspace, "token-a")
    workspace.request({"destination": "NONE"})
    take_lease(workspace.lease_path, "restore", workspace.clock())

    program.run_pass()
    assert workspace.restic_env.exists()

    workspace.clock.advance(600.0)
    program.run_pass()
    assert not workspace.restic_env.exists()
