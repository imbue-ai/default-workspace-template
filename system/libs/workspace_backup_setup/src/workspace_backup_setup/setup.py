"""Setting up a workspace's backups from credentials a client sends: initialise the repository, then write ``restic.env``.

A client outside the workspace (the Imbue Studio desktop, or the hosted web client) creates the
repository's storage, generates its password, and sends the whole ``restic.env``. This library
initialises the restic repository with those credentials and writes the file only once the
repository is ready: the ``host-backup`` service notices a changed ``restic.env`` within seconds
and may start a backup, so it must never see credentials for a repository that is not initialised.

The initialisation can outlast a request, so it runs in the background and its state is a file
under ``data/.state/``, which a client reads until it is ``READY`` or ``FAILED``. The state names
the process that runs the initialisation, and one left at ``INITIALIZING`` by a process that is gone
reads as ``FAILED``; sending the credentials again is safe, since an initialised repository that the
password opens is success.

While the initialisation runs, the credentials are kept in a file of mode 0600 beside ``restic.env``,
which is removed when it ends.
"""

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import auto
from pathlib import Path
from typing import Final

from host_backup.config import (
    RESTIC_ENV_PATH,
    missing_required_restic_keys,
    parse_restic_env_file,
)
from host_backup.restic import init_repo, run_restic
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

from workspace_backup_setup.errors import (
    BackupSetupRunningError,
    InvalidBackupCredentialsError,
)

PENDING_ENV_RELATIVE_PATH: Final[Path] = RESTIC_ENV_PATH.with_name(
    RESTIC_ENV_PATH.name + ".pending"
)
SETUP_STATE_RELATIVE_PATH: Final[Path] = Path("data/.state/backup_setup/state.json")
_SETUP_LOCK_RELATIVE_PATH: Final[Path] = SETUP_STATE_RELATIVE_PATH.with_name(
    "state.lock"
)
_SECRET_FILE_MODE: Final[int] = 0o600
# The stamp a ``restic.env`` moved aside carries, the one the Imbue Studio desktop gives its own archived copies.
ENV_ARCHIVE_TIMESTAMP_FORMAT: Final[str] = "%Y%m%dT%H%M%SZ"
_STATE_SCHEMA: Final[int] = 1
_OPEN_CHECK_TIMEOUT_SECONDS: Final[float] = 120.0
_DETAIL_MAX_CHARS: Final[int] = 500
# restic's own phrasing when the repository already exists, and when no key in it matches the password.
_ALREADY_INITIALIZED_MARKERS: Final[tuple[str, ...]] = (
    "already initialized",
    "already exists",
    "config file already exists",
)
_WRONG_PASSWORD_MARKER: Final[str] = "wrong password"
# ``/proc/<pid>/stat`` field 22, the process's start time, counted among the fields after the command name.
_START_TIME_FIELD_INDEX: Final[int] = 19

_GONE_INITIALIZER_DETAIL: Final[str] = (
    "The process that was initialising the repository is gone; send the credentials again."
)
_WRONG_PASSWORD_DETAIL: Final[str] = (
    "The repository is already initialized with a different password; the credentials that initialized it hold "
    "the only key."
)

# Runs the initialisation off the caller's thread; a test passes one that runs it in place.
BackgroundRunner = Callable[[Callable[[], None]], None]


class BackupSetupState(UpperCaseStrEnum):
    """How the initialisation of the workspace's backup repository stands."""

    NONE = auto()
    INITIALIZING = auto()
    READY = auto()
    FAILED = auto()


class BackupSetupStatus(FrozenModel):
    """The setup's state as a client reads it: no credential, and nothing about the process behind it."""

    state: BackupSetupState = Field(
        description="NONE until credentials are sent, then INITIALIZING, then READY or FAILED"
    )
    detail: str = Field(
        default="",
        description="Why the initialisation failed, in restic's own words; '' otherwise",
    )
    started_at: float | None = Field(
        default=None,
        description="When the initialisation began, seconds since the epoch; null when there was none",
    )
    finished_at: float | None = Field(
        default=None,
        description="When it ended, seconds since the epoch; null while it runs or when there was none",
    )


_NO_SETUP: Final[BackupSetupStatus] = BackupSetupStatus(state=BackupSetupState.NONE)


def _content_sha256(env_text: str) -> str:
    return hashlib.sha256(env_text.encode("utf-8")).hexdigest()


def _write_atomically(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, scratch = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(text)
    os.chmod(scratch, mode)
    os.replace(scratch, path)


def _process_start_time(proc_root: Path, pid: int) -> str:
    """When the process started, in clock ticks since boot, or '' where ``/proc`` does not have it."""
    try:
        stat_line = (proc_root / str(pid) / "stat").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return ""
    # The command name is parenthesized and may itself hold spaces and parentheses.
    fields = stat_line.rpartition(")")[2].split()
    return (
        fields[_START_TIME_FIELD_INDEX] if len(fields) > _START_TIME_FIELD_INDEX else ""
    )


def _is_initializer_live(record: Mapping[str, object], proc_root: Path) -> bool:
    """Whether the process a state record names is still there: alive, and started when the record says."""
    pid = record.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    recorded_start = str(record.get("pid_start_time") or "")
    return not recorded_start or _process_start_time(proc_root, pid) in (
        "",
        recorded_start,
    )


def _read_state_record(workspace_root: Path) -> dict[str, object] | None:
    try:
        raw: object = json.loads(
            (workspace_root / SETUP_STATE_RELATIVE_PATH).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    return (
        {str(key): value for key, value in raw.items()}
        if isinstance(raw, dict)
        else None
    )


def _write_state_record(workspace_root: Path, record: Mapping[str, object]) -> None:
    _write_atomically(
        workspace_root / SETUP_STATE_RELATIVE_PATH,
        json.dumps(dict(record), indent=2) + "\n",
        0o644,
    )


def _epoch(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _status_of(
    record: Mapping[str, object] | None, proc_root: Path
) -> BackupSetupStatus:
    if record is None:
        return _NO_SETUP
    try:
        state = BackupSetupState(str(record.get("state") or ""))
    except ValueError:
        return _NO_SETUP
    started_at = _epoch(record.get("started_at"))
    if state is BackupSetupState.INITIALIZING and not _is_initializer_live(
        record, proc_root
    ):
        return BackupSetupStatus(
            state=BackupSetupState.FAILED,
            detail=_GONE_INITIALIZER_DETAIL,
            started_at=started_at,
        )
    return BackupSetupStatus(
        state=state,
        detail=str(record.get("detail") or ""),
        started_at=started_at,
        finished_at=_epoch(record.get("finished_at")),
    )


def read_setup_status(workspace_root: Path, proc_root: Path) -> BackupSetupStatus:
    """How the initialisation stands; ``NONE`` when no credentials were ever sent through here."""
    return _status_of(_read_state_record(workspace_root), proc_root)


@contextmanager
def _setup_lock(workspace_root: Path) -> Generator[None, None, None]:
    """Serialize the check for an initialisation under way with the write that begins one."""
    lock_path = workspace_root / _SETUP_LOCK_RELATIVE_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _without_credentials(text: str, env_values: Mapping[str, str]) -> str:
    """restic's words with every value of the credentials struck out, cut to their last few lines."""
    redacted = text
    for value in sorted(
        {value for value in env_values.values() if value}, key=len, reverse=True
    ):
        redacted = redacted.replace(value, "[redacted]")
    return redacted.strip()[-_DETAIL_MAX_CHARS:]


def _initialize_repository(env_values: Mapping[str, str]) -> str | None:
    """Create the repository, or check that the password opens the one already there; why it could not, else None."""
    try:
        initialized = init_repo(env_values)
    except subprocess.TimeoutExpired:
        return "restic init did not finish in time."
    except OSError as e:
        return f"restic could not be run: {e}"
    if initialized.returncode == 0:
        return None
    combined = (initialized.stdout + initialized.stderr).lower()
    if not any(marker in combined for marker in _ALREADY_INITIALIZED_MARKERS):
        words = _without_credentials(initialized.stderr, env_values)
        return f"restic init failed (exit {initialized.returncode}): {words}"
    try:
        opened = run_restic(
            ("--no-lock", "cat", "config"),
            env_overrides=env_values,
            timeout_seconds=_OPEN_CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "restic did not finish checking the repository's password in time."
    except OSError as e:
        return f"restic could not be run: {e}"
    if opened.returncode == 0:
        return None
    if _WRONG_PASSWORD_MARKER in opened.stderr.lower():
        return _WRONG_PASSWORD_DETAIL
    words = _without_credentials(opened.stderr, env_values)
    return f"restic could not open the repository (exit {opened.returncode}): {words}"


def _install_restic_env(workspace_root: Path, env_text: str, now: datetime) -> None:
    """Write ``restic.env``: one with the same contents is left alone, one with other contents is moved aside first."""
    path = workspace_root / RESTIC_ENV_PATH
    try:
        existing_text: str | None = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing_text = None
    if existing_text == env_text:
        # Untouched, so its mtime does not move and the backup service starts no extra backup.
        os.chmod(path, _SECRET_FILE_MODE)
        return
    if existing_text is not None:
        os.replace(
            path,
            path.with_name(f"{path.name}.{now.strftime(ENV_ARCHIVE_TIMESTAMP_FORMAT)}"),
        )
    _write_atomically(path, env_text, _SECRET_FILE_MODE)


def _finish_setup(
    workspace_root: Path, env_text: str, started_at: float, clock: Callable[[], float]
) -> None:
    """Initialise the repository and record how it went; ``restic.env`` is written only when it is ready."""
    env_values = parse_restic_env_file(env_text)
    failure = _initialize_repository(env_values)
    if failure is None:
        try:
            _install_restic_env(
                workspace_root, env_text, datetime.fromtimestamp(clock(), timezone.utc)
            )
        except OSError as e:
            failure = (
                f"The repository is ready, but restic.env could not be written: {e}"
            )
    with _setup_lock(workspace_root):
        _write_state_record(
            workspace_root,
            {
                "schema": _STATE_SCHEMA,
                "state": (
                    BackupSetupState.READY
                    if failure is None
                    else BackupSetupState.FAILED
                ).value,
                "detail": failure or "",
                "started_at": started_at,
                "finished_at": clock(),
                "env_sha256": _content_sha256(env_text),
            },
        )
        (workspace_root / PENDING_ENV_RELATIVE_PATH).unlink(missing_ok=True)


def begin_setup(
    workspace_root: Path,
    env_text: str,
    *,
    proc_root: Path,
    run_in_background: BackgroundRunner,
    clock: Callable[[], float],
) -> BackupSetupStatus:
    """Start initialising the repository ``env_text`` names, and answer the setup's state without waiting.

    Raises ``InvalidBackupCredentialsError`` for text that does not set a repository and its password, and
    ``BackupSetupRunningError`` while another initialisation runs. Credentials already in place and ready
    change nothing and answer ``READY``.
    """
    missing = missing_required_restic_keys(parse_restic_env_file(env_text))
    if missing:
        raise InvalidBackupCredentialsError(
            f"The credentials do not set {' and '.join(missing)}."
        )
    with _setup_lock(workspace_root):
        record = _read_state_record(workspace_root)
        current = _status_of(record, proc_root)
        if current.state is BackupSetupState.INITIALIZING:
            raise BackupSetupRunningError(
                "A backup repository is being initialised already."
            )
        try:
            installed_text: str | None = (workspace_root / RESTIC_ENV_PATH).read_text(
                encoding="utf-8"
            )
        except OSError:
            installed_text = None
        if installed_text == env_text and current.state is BackupSetupState.READY:
            return current
        started_at = clock()
        own_pid = os.getpid()
        _write_atomically(
            workspace_root / PENDING_ENV_RELATIVE_PATH, env_text, _SECRET_FILE_MODE
        )
        _write_state_record(
            workspace_root,
            {
                "schema": _STATE_SCHEMA,
                "state": BackupSetupState.INITIALIZING.value,
                "detail": "",
                "started_at": started_at,
                "finished_at": None,
                "pid": own_pid,
                "pid_start_time": _process_start_time(proc_root, own_pid),
                "env_sha256": _content_sha256(env_text),
            },
        )
    run_in_background(
        lambda: _finish_setup(workspace_root, env_text, started_at, clock)
    )
    return BackupSetupStatus(state=BackupSetupState.INITIALIZING, started_at=started_at)


def remove_credentials(workspace_root: Path, now: datetime, *, proc_root: Path) -> bool:
    """Move ``restic.env`` aside, so the backup service goes idle; whether there was one to move.

    The moved copy keeps the old repository reachable by hand and cannot be adopted again, and the
    setup's state starts over. Raises ``BackupSetupRunningError`` while an initialisation runs, which
    would write the file back.
    """
    path = workspace_root / RESTIC_ENV_PATH
    with _setup_lock(workspace_root):
        if (
            _status_of(_read_state_record(workspace_root), proc_root).state
            is BackupSetupState.INITIALIZING
        ):
            raise BackupSetupRunningError(
                "A backup repository is being initialised; wait for it to finish."
            )
        try:
            os.replace(
                path,
                path.with_name(
                    f"{path.name}.{now.strftime(ENV_ARCHIVE_TIMESTAMP_FORMAT)}"
                ),
            )
        except FileNotFoundError:
            is_removed = False
        else:
            is_removed = True
        (workspace_root / SETUP_STATE_RELATIVE_PATH).unlink(missing_ok=True)
    return is_removed
