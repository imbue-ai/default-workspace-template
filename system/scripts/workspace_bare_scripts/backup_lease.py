"""The workspace's backup lease: what keeps two backup operations from running in it at once.

A backup operation is anything that changes where the workspace backs up to or what it holds of its
backups: the workspace's own setup (``backup-setup``), and the Imbue Studio desktop's restore, its
update of the backup service and its migration of the backup store. Each takes the lease before it
starts, and one that finds it held refuses to start and says who holds it.

The lease is a file that names its holder, when it was taken, and when it expires. It is under
``/run``, so it outlasts the workspace's services being stopped (which a restore does) and does not
outlast a reboot, after which nothing that held it is running; it is not under ``data/``, which a
backup would carry away and a restore put back. It lasts ten minutes from when it was taken or last
renewed, and one past its expiry is free, so a holder that died does not block for ever.

Usage, from the repo root (the cwd ``mngr exec`` gives every command there)::

    python3 system/scripts/backup_lease.py take --holder restore
    python3 system/scripts/backup_lease.py renew --holder restore
    python3 system/scripts/backup_lease.py release --holder restore
    python3 system/scripts/backup_lease.py read
    python3 system/scripts/backup_lease.py hold --holder restore [--keep] -- <command...>

Every command prints one JSON line: the lease as it stands (``{"holder", "taken_at",
"expires_at"}``, times in seconds since the epoch) or ``null`` when nobody holds it. ``take`` and
``renew`` exit 0 when the caller holds the lease afterwards, and 75 when it does not: someone else
holds it (the line printed names them), or, for ``renew``, the caller's own lease ran out.
``release`` gives back only the caller's own lease and always exits 0. ``hold`` takes the lease (or
renews the caller's own), runs the command, renews every minute for as long as it runs, gives the
lease back when it ends however it ends (``--keep`` leaves it held, renewed, for a program with more
to run), and exits with the command's exit code, or 75 without running it when someone else holds
the lease.

Standard library only: the Imbue Studio desktop runs this by its path with no venv, and the shell
and ``backup-setup`` import it.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import NamedTuple

DEFAULT_LEASE_PATH = "/run/backup-operation.lease"
# Names another file to keep the lease in; only a test sets it.
LEASE_PATH_ENV = "BACKUP_LEASE_PATH"
LEASE_SECONDS = 600.0
HOLD_RENEW_INTERVAL_SECONDS = 60.0

# The holder the workspace's own setup takes the lease as.
HOLDER_SETUP = "setup"

EXIT_OK = 0
EXIT_USAGE = 2
# EX_TEMPFAIL: the caller does not hold the lease, and may try again once it is free.
EXIT_NOT_HELD = 75

_LEASE_FILE_MODE = 0o644


class BackupLease(NamedTuple):
    """Who holds the backup lease, since when, and until when."""

    holder: str
    taken_at: float
    expires_at: float

    def document(self) -> dict[str, object]:
        return {
            "holder": self.holder,
            "taken_at": self.taken_at,
            "expires_at": self.expires_at,
        }


class BackupLeaseHeldError(Exception):
    """Someone else holds the backup lease."""

    def __init__(self, lease: BackupLease) -> None:
        super().__init__(
            f"The backup lease is held by {lease.holder!r} until {lease.expires_at:.0f}."
        )
        self.lease = lease


class BackupLeaseNotHeldError(Exception):
    """The caller does not hold the backup lease it asked to renew: it ran out, or was never taken."""


def default_lease_path() -> Path:
    return Path(os.environ.get(LEASE_PATH_ENV) or DEFAULT_LEASE_PATH)


@contextmanager
def _lease_lock(lease_path: Path) -> Generator[None, None, None]:
    """Serialize a read of the lease with the write that follows it."""
    lock_path = lease_path.with_name(lease_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _stored_lease(lease_path: Path) -> BackupLease | None:
    """The lease the file names, whether or not it has run out; None when there is none to read."""
    try:
        raw: object = json.loads(lease_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    holder = raw.get("holder")
    taken_at = _number(raw.get("taken_at"))
    expires_at = _number(raw.get("expires_at"))
    if (
        not isinstance(holder, str)
        or not holder
        or taken_at is None
        or expires_at is None
    ):
        return None
    return BackupLease(holder=holder, taken_at=taken_at, expires_at=expires_at)


def _write_lease(lease_path: Path, lease: BackupLease) -> None:
    handle, scratch = tempfile.mkstemp(
        dir=lease_path.parent, prefix=f".{lease_path.name}."
    )
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(json.dumps(lease.document()) + "\n")
    os.chmod(scratch, _LEASE_FILE_MODE)
    os.replace(scratch, lease_path)


def read_lease(lease_path: Path, now: float) -> BackupLease | None:
    """Who holds the lease at ``now``; None when nobody does, which a lease past its expiry also is."""
    lease = _stored_lease(lease_path)
    if lease is None or lease.expires_at <= now:
        return None
    return lease


def take_lease(lease_path: Path, holder: str, now: float) -> BackupLease:
    """Take the lease for ``holder``. Raises ``BackupLeaseHeldError`` while anyone holds it, ``holder`` included."""
    with _lease_lock(lease_path):
        current = read_lease(lease_path, now)
        if current is not None:
            raise BackupLeaseHeldError(current)
        lease = BackupLease(holder=holder, taken_at=now, expires_at=now + LEASE_SECONDS)
        _write_lease(lease_path, lease)
        return lease


def renew_lease(lease_path: Path, holder: str, now: float) -> BackupLease:
    """Make ``holder``'s lease last ten minutes from ``now``.

    Raises ``BackupLeaseHeldError`` when someone else holds the lease, and ``BackupLeaseNotHeldError``
    when nobody does: a lease that ran out is not renewed, since another operation may have run since.
    """
    with _lease_lock(lease_path):
        current = read_lease(lease_path, now)
        if current is None:
            raise BackupLeaseNotHeldError(
                f"{holder!r} does not hold the backup lease: it ran out, or was never taken."
            )
        if current.holder != holder:
            raise BackupLeaseHeldError(current)
        lease = current._replace(expires_at=now + LEASE_SECONDS)
        _write_lease(lease_path, lease)
        return lease


def release_lease(lease_path: Path, holder: str, now: float) -> bool:
    """Give ``holder``'s lease back; whether there was one. Someone else's lease is left alone."""
    with _lease_lock(lease_path):
        current = read_lease(lease_path, now)
        if current is None or current.holder != holder:
            return False
        lease_path.unlink(missing_ok=True)
        return True


def take_or_renew_lease(lease_path: Path, holder: str, now: float) -> BackupLease:
    """The lease for ``holder``, taken when it is free and renewed when ``holder`` holds it already.

    For a holder that knows it is the only one of its name. Raises ``BackupLeaseHeldError`` when someone
    else holds it.
    """
    try:
        return renew_lease(lease_path, holder, now)
    except BackupLeaseNotHeldError:
        return take_lease(lease_path, holder, now)


def hold_lease(
    lease_path: Path,
    holder: str,
    command: Sequence[str],
    *,
    renew_interval_seconds: float,
    clock: Callable[[], float],
    is_kept: bool,
) -> int:
    """Run ``command`` holding the lease, renewed every ``renew_interval_seconds`` for as long as it runs.

    Answers the command's exit code. The lease is given back when the command ends, however it ends,
    unless ``is_kept``. Raises ``BackupLeaseHeldError``, without running the command, when someone else
    holds the lease.
    """
    take_or_renew_lease(lease_path, holder, clock())
    try:
        return _run_renewing(lease_path, holder, command, renew_interval_seconds, clock)
    finally:
        if is_kept:
            _renew_while_holding(lease_path, holder, clock())
        else:
            release_lease(lease_path, holder, clock())


def _run_renewing(
    lease_path: Path,
    holder: str,
    command: Sequence[str],
    renew_interval_seconds: float,
    clock: Callable[[], float],
) -> int:
    """Run ``command`` to its end, renewing ``holder``'s lease as it runs; its exit code."""
    process = subprocess.Popen(list(command))

    def _pass_on(signal_number: int, _frame: FrameType | None) -> None:
        process.send_signal(signal_number)

    previous_handlers = {
        signal_number: signal.signal(signal_number, _pass_on)
        for signal_number in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        while process.poll() is None:
            try:
                process.wait(timeout=renew_interval_seconds)
            except subprocess.TimeoutExpired:
                _renew_while_holding(lease_path, holder, clock())
        return process.returncode
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)


def _renew_while_holding(lease_path: Path, holder: str, now: float) -> None:
    """Renew ``holder``'s lease under a running command, which a lost lease must not stop half way."""
    try:
        take_or_renew_lease(lease_path, holder, now)
    except BackupLeaseHeldError as e:
        sys.stderr.write(f"backup-lease: could not renew for {holder!r}: {e}\n")


def _print_lease(lease: BackupLease | None) -> None:
    sys.stdout.write(json.dumps(lease.document() if lease is not None else None) + "\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backup-lease",
        description="Take, renew, give back or read the workspace's backup lease.",
    )
    parser.add_argument(
        "--lease-path",
        type=Path,
        default=None,
        help=f"The lease's file (default: ${LEASE_PATH_ENV}, else {DEFAULT_LEASE_PATH})",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (
        ("take", "Take the lease; exit 75 when anyone holds it."),
        (
            "renew",
            "Renew the holder's own lease; exit 75 when it ran out or someone else holds it.",
        ),
        ("release", "Give the holder's own lease back."),
    ):
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument(
            "--holder", required=True, help="Who holds the lease, e.g. restore"
        )
    commands.add_parser(
        "read", help="Print who holds the lease.", description="Print who holds it."
    )
    hold = commands.add_parser(
        "hold",
        help="Run a command holding the lease, renewed every minute while it runs.",
        description="Run a command holding the lease, renewed every minute while it runs.",
    )
    hold.add_argument(
        "--holder", required=True, help="Who holds the lease, e.g. restore"
    )
    hold.add_argument(
        "--keep",
        action="store_true",
        help="Leave the lease held when the command ends, for a program with more to run",
    )
    hold.add_argument(
        "held_command", nargs=argparse.REMAINDER, help="-- and the command to run"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    lease_path: Path = arguments.lease_path or default_lease_path()
    now = time.time()
    if arguments.command == "read":
        _print_lease(read_lease(lease_path, now))
        return EXIT_OK
    holder: str = arguments.holder
    if arguments.command == "release":
        release_lease(lease_path, holder, now)
        _print_lease(read_lease(lease_path, now))
        return EXIT_OK
    try:
        if arguments.command == "take":
            _print_lease(take_lease(lease_path, holder, now))
            return EXIT_OK
        if arguments.command == "renew":
            _print_lease(renew_lease(lease_path, holder, now))
            return EXIT_OK
        held_command = list(arguments.held_command)
        if held_command[:1] == ["--"]:
            held_command = held_command[1:]
        if not held_command:
            sys.stderr.write("backup-lease: hold needs a command after --\n")
            return EXIT_USAGE
        return hold_lease(
            lease_path,
            holder,
            held_command,
            renew_interval_seconds=HOLD_RENEW_INTERVAL_SECONDS,
            clock=time.time,
            is_kept=arguments.keep,
        )
    except BackupLeaseHeldError as e:
        sys.stderr.write(f"backup-lease: {e}\n")
        _print_lease(e.lease)
        return EXIT_NOT_HELD
    except BackupLeaseNotHeldError as e:
        sys.stderr.write(f"backup-lease: {e}\n")
        _print_lease(None)
        return EXIT_NOT_HELD


if __name__ == "__main__":
    raise SystemExit(main())
