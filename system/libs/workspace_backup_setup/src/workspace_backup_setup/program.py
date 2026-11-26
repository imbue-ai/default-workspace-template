"""``backup-setup``: the supervised program that sets up a workspace's backups.

A destination is named through ``destination.request_backup_destination``. This program does the rest: it makes
the repository's password unless the workspace kept one for that repository, initialises the repository (or checks
that the password opens one already there), and installs ``restic.env`` only then, since the ``host-backup``
service starts backing up within seconds of the file changing. A step that fails is tried again after a minute,
then after twice as long each time, up to an hour. For ``NONE`` it moves ``restic.env`` aside and keeps it.

It is a program of its own because neither of its neighbours can carry the work: the backup service is replaced by
the Imbue Studio desktop with the version that desktop pins, and the shell is restarted by an update and stopped by
a restore. What it was asked is on disk, so it takes its work up again whenever it starts.

Once an hour, and when it starts, it checks that the credentials in use still open the repository. A storage that
refuses them leaves the setup awaiting storage, with ``restic.env`` where it is: the file holds the repository's
password, and the backup service's failing runs harm nothing.

It calls the backup service only through what every version of that service has: ``init_repo``, ``run_restic``
with its ``env_overrides`` and ``timeout_seconds``, and the reader of ``restic.env``.
"""

import os
import secrets
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import FrameType
from typing import Final

from host_backup.config import parse_restic_env_file
from host_backup.restic import init_repo, run_restic
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from loguru import logger
from pydantic import Field
from workspace_bare_scripts.backup_lease import (
    HOLDER_SETUP,
    BackupLeaseHeldError,
    release_lease,
    take_or_renew_lease,
)

from workspace_backup_setup.env_files import (
    PASSWORD_KEY,
    PENDING_ENV_RELATIVE_PATH,
    REPOSITORY_KEY,
    BackupDestinationKind,
    env_digest,
    install_restic_env,
    keep_restic_env_aside,
    kept_password_for_repository,
    read_pending_env,
    read_restic_env_text,
    render_restic_env,
    repository_without_credentials,
    storage_of,
    write_pending_env,
)
from workspace_backup_setup.state import (
    NO_STORAGE_DETAIL,
    BackupSetupState,
    SetupRecord,
    backup_lease_path,
    failed_record,
    read_setup_record,
    setup_lock,
    take_another_writers_file,
    workspace_id_from_environment,
    write_setup_record,
)

POLL_SECONDS: Final[float] = 1.0
CREDENTIALS_CHECK_INTERVAL_SECONDS: Final[float] = 3600.0
_PASSWORD_BYTES: Final[int] = 32
_OPEN_CHECK_TIMEOUT_SECONDS: Final[float] = 120.0
_DETAIL_MAX_CHARS: Final[int] = 500
# restic's own phrasing when the repository already exists, and when no key in it matches the password.
_ALREADY_INITIALIZED_MARKERS: Final[tuple[str, ...]] = (
    "already initialized",
    "already exists",
    "config file already exists",
)
_WRONG_PASSWORD_MARKER: Final[str] = "wrong password"
# How a storage says it does not accept the credentials it was shown, in restic's words and in the storage's own:
# the backup store's and any REST server's 401 or 403, and an S3 storage's codes with restic's rendering of them.
_STORAGE_REFUSAL_MARKERS: Final[tuple[str, ...]] = (
    "(401)",
    "(403)",
    "unauthorized",
    "access denied",
    "accessdenied",
    "invalidaccesskeyid",
    "invalid access key",
    "signaturedoesnotmatch",
    "request signature we calculated does not match",
)

_WRONG_PASSWORD_DETAIL: Final[str] = (
    "The repository is already initialized with a password this workspace does not hold."
)
_STORAGE_REFUSED_DETAIL: Final[str] = (
    "The storage does not accept the credentials this workspace holds for it."
)


class _ResticFailure(FrozenModel):
    """Why restic could not do what it was asked, with no credential in the words."""

    detail: str = Field(description="What failed")
    is_storage_refusal: bool = Field(
        default=False,
        description="Whether the storage refused the credentials it was shown",
    )


def _make_password() -> str:
    return secrets.token_urlsafe(_PASSWORD_BYTES)


def _without_credentials(text: str, env_values: Mapping[str, str]) -> str:
    """restic's words with every value of the credentials struck out, cut to their last few lines."""
    redacted = text
    for value in sorted(
        {value for value in env_values.values() if value}, key=len, reverse=True
    ):
        redacted = redacted.replace(value, "[redacted]")
    return redacted.strip()[-_DETAIL_MAX_CHARS:]


def _failure_of(
    what: str, result: subprocess.CompletedProcess[str], env_values: Mapping[str, str]
) -> _ResticFailure:
    combined = (result.stdout + result.stderr).lower()
    if any(marker in combined for marker in _STORAGE_REFUSAL_MARKERS):
        return _ResticFailure(detail=_STORAGE_REFUSED_DETAIL, is_storage_refusal=True)
    words = _without_credentials(result.stderr, env_values)
    return _ResticFailure(detail=f"{what} (exit {result.returncode}): {words}")


def _open_repository(env_values: Mapping[str, str]) -> _ResticFailure | None:
    """Check that the credentials open the repository; why they do not, else None."""
    try:
        opened = run_restic(
            ("--no-lock", "cat", "config"),
            env_overrides=env_values,
            timeout_seconds=_OPEN_CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _ResticFailure(
            detail="restic did not finish opening the repository in time."
        )
    except OSError as e:
        return _ResticFailure(detail=f"restic could not be run: {e}")
    if opened.returncode == 0:
        return None
    if _WRONG_PASSWORD_MARKER in opened.stderr.lower():
        return _ResticFailure(detail=_WRONG_PASSWORD_DETAIL)
    return _failure_of("restic could not open the repository", opened, env_values)


def _initialize_repository(env_values: Mapping[str, str]) -> _ResticFailure | None:
    """Create the repository, or check that the password opens the one already there; why not, else None."""
    try:
        initialized = init_repo(env_values)
    except subprocess.TimeoutExpired:
        return _ResticFailure(detail="restic init did not finish in time.")
    except OSError as e:
        return _ResticFailure(detail=f"restic could not be run: {e}")
    if initialized.returncode == 0:
        return None
    combined = (initialized.stdout + initialized.stderr).lower()
    if any(marker in combined for marker in _ALREADY_INITIALIZED_MARKERS):
        return _open_repository(env_values)
    return _failure_of("restic init failed", initialized, env_values)


class BackupSetupProgram:
    """One run of ``backup-setup``: what it holds in memory between passes is only when it next checks the
    credentials in use, so a new one takes up whatever the last one left."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        lease_path: Path,
        clock: Callable[[], float],
        workspace_id: str,
        make_password: Callable[[], str] = _make_password,
    ) -> None:
        self._workspace_root = workspace_root
        self._lease_path = lease_path
        self._clock = clock
        self._workspace_id = workspace_id
        self._make_password = make_password
        self._is_started = False
        self._next_credentials_check_at = clock()
        self._awaited_holder = ""

    def run_pass(self) -> None:
        """Do whatever is due now: a setup asked for or due another try, a turn-off, or the check of the
        credentials in use."""
        now = self._clock()
        is_first_pass = not self._is_started
        if is_first_pass:
            # No other program takes the lease under this name, so one still held is a previous run's.
            release_lease(self._lease_path, HOLDER_SETUP, now)
            self._is_started = True
        with setup_lock(self._workspace_root):
            record = take_another_writers_file(
                self._workspace_root, now, self._workspace_id
            )
        env_text = read_restic_env_text(self._workspace_root)
        if record.destination is BackupDestinationKind.NONE:
            if env_text is not None:
                self._turn_off(record)
            return
        pending = read_pending_env(self._workspace_root, record.request_id)
        is_attempt_due = record.state is BackupSetupState.INITIALIZING or (
            record.state is BackupSetupState.FAILED
            and (is_first_pass or record.retry_at is None or record.retry_at <= now)
        )
        is_check_due = (
            record.state in (BackupSetupState.READY, BackupSetupState.AWAITING_STORAGE)
            and now >= self._next_credentials_check_at
        )
        if is_check_due:
            self._next_credentials_check_at = now + CREDENTIALS_CHECK_INTERVAL_SECONDS
        if is_attempt_due or (is_check_due and pending is not None):
            self._attempt(record, pending)
        elif is_check_due and env_text is not None:
            in_use = parse_restic_env_file(env_text)
            # A destination that awaits its first storage says nothing of the previous one's file, still in place.
            if record.repository == repository_without_credentials(
                in_use.get(REPOSITORY_KEY, "")
            ):
                self._check_credentials_in_use(record, in_use)

    def _note_waiting_for(self, holder: str) -> None:
        """Say once, not every pass, that the work waits for whoever holds the backup lease."""
        if holder != self._awaited_holder:
            logger.info(
                "The backup setup waits for {} to give the backup lease back", holder
            )
            self._awaited_holder = holder

    def _write_if_still_asked(
        self, record: SetupRecord, new_record: SetupRecord
    ) -> bool:
        """Write ``new_record`` unless another destination was named since ``record`` was read; whether it was
        written. The caller holds ``setup_lock``."""
        current = read_setup_record(self._workspace_root)
        if current.request_id != record.request_id:
            return False
        write_setup_record(self._workspace_root, new_record)
        return True

    def _turn_off(self, record: SetupRecord) -> None:
        now = self._clock()
        try:
            take_or_renew_lease(self._lease_path, HOLDER_SETUP, now)
        except BackupLeaseHeldError as e:
            self._note_waiting_for(e.lease.holder)
            return
        self._awaited_holder = ""
        try:
            with setup_lock(self._workspace_root):
                if (
                    read_setup_record(self._workspace_root).request_id
                    != record.request_id
                ):
                    return
                keep_restic_env_aside(self._workspace_root, now)
                write_setup_record(
                    self._workspace_root, SetupRecord(request_id=record.request_id)
                )
            logger.info("Turned backups off: restic.env is kept aside")
        finally:
            release_lease(self._lease_path, HOLDER_SETUP, self._clock())

    def _attempt(self, record: SetupRecord, pending: dict[str, str] | None) -> None:
        now = self._clock()
        if pending is None or not pending.get(REPOSITORY_KEY):
            with setup_lock(self._workspace_root):
                self._write_if_still_asked(
                    record,
                    SetupRecord(
                        request_id=record.request_id,
                        destination=record.destination,
                        state=BackupSetupState.AWAITING_STORAGE,
                        detail=NO_STORAGE_DETAIL,
                        installed_digest=record.installed_digest,
                    ),
                )
            return
        try:
            take_or_renew_lease(self._lease_path, HOLDER_SETUP, now)
        except BackupLeaseHeldError as e:
            self._note_waiting_for(e.lease.holder)
            return
        self._awaited_holder = ""
        try:
            self._attempt_holding_the_lease(record, pending)
        finally:
            release_lease(self._lease_path, HOLDER_SETUP, self._clock())

    def _attempt_holding_the_lease(
        self, record: SetupRecord, pending: dict[str, str]
    ) -> None:
        started_at = self._clock()
        attempting = record.model_copy_update(
            to_update(record.field_ref().state, BackupSetupState.INITIALIZING),
            to_update(record.field_ref().detail, ""),
            to_update(record.field_ref().started_at, started_at),
            to_update(record.field_ref().finished_at, None),
            to_update(record.field_ref().retry_at, None),
        )
        with setup_lock(self._workspace_root):
            if not self._write_if_still_asked(record, attempting):
                return
        repository = pending[REPOSITORY_KEY]
        in_use_text = read_restic_env_text(self._workspace_root)
        in_use = parse_restic_env_file(in_use_text) if in_use_text is not None else {}
        if in_use.get(REPOSITORY_KEY) == repository:
            # A repository at the backup store has one token, so two clients that each ask for one end the
            # other's. Whichever the storage still accepts is the one to keep.
            refusal = (
                _open_repository(in_use)
                if storage_of(in_use) != storage_of(pending)
                else None
            )
            if refusal is None:
                self._keep_the_credentials_in_use(attempting, in_use_text or "")
                return
            if not refusal.is_storage_refusal:
                self._record_failure(
                    attempting,
                    _ResticFailure(
                        detail=f"The credentials in use could not be checked: {refusal.detail}"
                    ),
                )
                return
        if not pending.get(PASSWORD_KEY):
            password = kept_password_for_repository(self._workspace_root, repository)
            pending = {
                **pending,
                PASSWORD_KEY: password
                if password is not None
                else self._make_password(),
            }
            # Kept before it is used: a repository initialised with a password that was then lost cannot be read.
            write_pending_env(self._workspace_root, record.request_id, pending)
        self._renew()
        failure = _initialize_repository(pending)
        if failure is not None:
            self._record_failure(attempting, failure)
            return
        self._renew()
        env_text = render_restic_env(pending)
        now = self._clock()
        with setup_lock(self._workspace_root):
            if read_setup_record(self._workspace_root).request_id != record.request_id:
                return
            try:
                install_restic_env(self._workspace_root, env_text, now)
            except OSError as e:
                write_setup_record(
                    self._workspace_root,
                    failed_record(
                        attempting,
                        f"The repository is ready, but restic.env could not be written: {e}",
                        now,
                    ),
                )
                return
            (self._workspace_root / PENDING_ENV_RELATIVE_PATH).unlink(missing_ok=True)
            write_setup_record(
                self._workspace_root,
                SetupRecord(
                    request_id=record.request_id,
                    destination=record.destination,
                    repository=repository_without_credentials(repository),
                    state=BackupSetupState.READY,
                    started_at=started_at,
                    finished_at=now,
                    installed_digest=env_digest(env_text),
                ),
            )
        self._next_credentials_check_at = now + CREDENTIALS_CHECK_INTERVAL_SECONDS
        logger.info("Backups are set up: restic.env is in place")

    def _renew(self) -> None:
        try:
            take_or_renew_lease(self._lease_path, HOLDER_SETUP, self._clock())
        except BackupLeaseHeldError as e:
            logger.warning(
                "The backup lease ran out under the setup and {} took it",
                e.lease.holder,
            )

    def _keep_the_credentials_in_use(
        self, attempting: SetupRecord, in_use_text: str
    ) -> None:
        now = self._clock()
        with setup_lock(self._workspace_root):
            if (
                read_setup_record(self._workspace_root).request_id
                != attempting.request_id
            ):
                return
            (self._workspace_root / PENDING_ENV_RELATIVE_PATH).unlink(missing_ok=True)
            write_setup_record(
                self._workspace_root,
                SetupRecord(
                    request_id=attempting.request_id,
                    destination=attempting.destination,
                    repository=attempting.repository,
                    state=BackupSetupState.READY,
                    started_at=attempting.started_at,
                    finished_at=now,
                    installed_digest=env_digest(in_use_text),
                ),
            )
        self._next_credentials_check_at = now + CREDENTIALS_CHECK_INTERVAL_SECONDS
        logger.info(
            "The storage still accepts the credentials in use; the ones offered are let go"
        )

    def _record_failure(self, attempting: SetupRecord, failure: _ResticFailure) -> None:
        now = self._clock()
        if failure.is_storage_refusal:
            new_record = attempting.model_copy_update(
                to_update(
                    attempting.field_ref().state, BackupSetupState.AWAITING_STORAGE
                ),
                to_update(attempting.field_ref().detail, failure.detail),
                to_update(attempting.field_ref().finished_at, now),
                to_update(attempting.field_ref().failed_attempts, 0),
            )
            self._next_credentials_check_at = now + CREDENTIALS_CHECK_INTERVAL_SECONDS
        else:
            new_record = failed_record(attempting, failure.detail, now)
        with setup_lock(self._workspace_root):
            self._write_if_still_asked(attempting, new_record)
        logger.warning("The backup setup did not finish: {}", failure.detail)

    def _check_credentials_in_use(
        self, record: SetupRecord, in_use: Mapping[str, str]
    ) -> None:
        refusal = _open_repository(in_use)
        if refusal is None:
            new_state, detail = BackupSetupState.READY, ""
        elif refusal.is_storage_refusal:
            new_state, detail = BackupSetupState.AWAITING_STORAGE, refusal.detail
        else:
            logger.info(
                "Could not check the credentials in use, which changes nothing: {}",
                refusal.detail,
            )
            return
        if new_state is record.state:
            return
        with setup_lock(self._workspace_root):
            self._write_if_still_asked(
                record,
                record.model_copy_update(
                    to_update(record.field_ref().state, new_state),
                    to_update(record.field_ref().detail, detail),
                ),
            )
        logger.info("The check of the credentials in use reads {}", new_state.value)


def main() -> None:
    """Run ``backup-setup`` for the workspace at the working directory until it is told to stop."""
    program = BackupSetupProgram(
        Path.cwd(),
        lease_path=backup_lease_path(),
        clock=time.time,
        workspace_id=workspace_id_from_environment(os.environ),
    )
    is_stopping = threading.Event()

    def _stop(_signal_number: int, _frame: FrameType | None) -> None:
        is_stopping.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logger.info("Starting backup-setup")
    _run_pass_and_log(program)
    while not is_stopping.wait(POLL_SECONDS):
        _run_pass_and_log(program)
    logger.info("Stopping backup-setup")


def _run_pass_and_log(program: BackupSetupProgram) -> None:
    try:
        program.run_pass()
    except OSError as e:
        logger.opt(exception=e).error("A pass of backup-setup failed")
