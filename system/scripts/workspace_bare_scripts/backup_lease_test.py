"""Tests for the backup lease: one taker at a time, renewed, run out, given back, and held around a command."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from workspace_bare_scripts import backup_lease
from workspace_bare_scripts.backup_lease import (
    EXIT_NOT_HELD,
    LEASE_SECONDS,
    BackupLease,
    BackupLeaseHeldError,
    BackupLeaseNotHeldError,
    hold_lease,
    read_lease,
    release_lease,
    renew_lease,
    take_lease,
    take_or_renew_lease,
)

_TAKEN_AT = 1_795_000_000.0
# Exits 0 once the lease file's contents change under it, and 1 when they never do.
_WAIT_FOR_A_RENEWAL = (
    'first=$(cat "$1"); i=0; '
    'while [ "$(cat "$1")" = "$first" ]; do i=$((i+1)); [ $i -gt 800 ] && exit 1; sleep 0.01; done'
)
_STUB_PATH = Path(__file__).resolve().parents[1] / "backup_lease.py"


@pytest.fixture
def lease_path(tmp_path: Path) -> Path:
    return tmp_path / "run" / "backup-operation.lease"


class _TickingClock:
    """A clock that reads a second later each time it is asked."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def test_nobody_holds_a_lease_that_was_never_taken(lease_path: Path) -> None:
    assert read_lease(lease_path, _TAKEN_AT) is None


def test_a_taken_lease_names_its_holder_and_lasts_ten_minutes(lease_path: Path) -> None:
    taken = take_lease(lease_path, "restore", _TAKEN_AT)

    assert taken == BackupLease(
        holder="restore", taken_at=_TAKEN_AT, expires_at=_TAKEN_AT + 600.0
    )
    assert read_lease(lease_path, _TAKEN_AT + 599.0) == taken
    assert json.loads(lease_path.read_text()) == {
        "holder": "restore",
        "taken_at": _TAKEN_AT,
        "expires_at": _TAKEN_AT + 600.0,
    }


def test_a_held_lease_is_refused_to_everyone_and_names_its_holder(
    lease_path: Path,
) -> None:
    taken = take_lease(lease_path, "restore", _TAKEN_AT)

    with pytest.raises(BackupLeaseHeldError) as refused:
        take_lease(lease_path, "setup", _TAKEN_AT + 1.0)
    with pytest.raises(BackupLeaseHeldError):
        take_lease(lease_path, "restore", _TAKEN_AT + 1.0)

    assert refused.value.lease == taken
    assert "'restore'" in str(refused.value)
    assert read_lease(lease_path, _TAKEN_AT + 1.0) == taken


def test_a_renewal_makes_the_lease_last_ten_minutes_from_then(lease_path: Path) -> None:
    take_lease(lease_path, "restore", _TAKEN_AT)

    renewed = renew_lease(lease_path, "restore", _TAKEN_AT + 500.0)

    assert renewed == BackupLease(
        holder="restore",
        taken_at=_TAKEN_AT,
        expires_at=_TAKEN_AT + 500.0 + LEASE_SECONDS,
    )
    assert read_lease(lease_path, _TAKEN_AT + 1000.0) == renewed


def test_a_lease_past_its_expiry_is_free_and_is_not_renewed(lease_path: Path) -> None:
    take_lease(lease_path, "restore", _TAKEN_AT)
    after_expiry = _TAKEN_AT + LEASE_SECONDS

    assert read_lease(lease_path, after_expiry) is None
    with pytest.raises(BackupLeaseNotHeldError):
        renew_lease(lease_path, "restore", after_expiry)
    assert take_lease(lease_path, "setup", after_expiry).holder == "setup"


def test_only_the_holder_renews_or_gives_back_its_lease(lease_path: Path) -> None:
    taken = take_lease(lease_path, "restore", _TAKEN_AT)

    with pytest.raises(BackupLeaseHeldError):
        renew_lease(lease_path, "setup", _TAKEN_AT + 1.0)
    assert release_lease(lease_path, "setup", _TAKEN_AT + 1.0) is False
    assert read_lease(lease_path, _TAKEN_AT + 1.0) == taken

    assert release_lease(lease_path, "restore", _TAKEN_AT + 2.0) is True
    assert read_lease(lease_path, _TAKEN_AT + 2.0) is None
    assert release_lease(lease_path, "restore", _TAKEN_AT + 3.0) is False


def test_a_holder_that_is_the_only_one_of_its_name_takes_its_own_lease_again(
    lease_path: Path,
) -> None:
    take_lease(lease_path, "setup", _TAKEN_AT)

    again = take_or_renew_lease(lease_path, "setup", _TAKEN_AT + 60.0)

    assert again == BackupLease(
        holder="setup", taken_at=_TAKEN_AT, expires_at=_TAKEN_AT + 60.0 + LEASE_SECONDS
    )
    with pytest.raises(BackupLeaseHeldError):
        take_or_renew_lease(lease_path, "restore", _TAKEN_AT + 61.0)


def test_a_file_that_is_no_lease_reads_as_free(lease_path: Path) -> None:
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text("not a lease")

    assert read_lease(lease_path, _TAKEN_AT) is None
    assert take_lease(lease_path, "restore", _TAKEN_AT).holder == "restore"


def test_hold_renews_while_its_command_runs_and_gives_the_lease_back(
    lease_path: Path,
) -> None:
    clock = _TickingClock(_TAKEN_AT)

    exit_code = hold_lease(
        lease_path,
        "restore",
        ["sh", "-c", _WAIT_FOR_A_RENEWAL, "sh", str(lease_path)],
        renew_interval_seconds=0.01,
        clock=clock,
        is_kept=False,
    )

    assert exit_code == 0
    assert read_lease(lease_path, clock.now) is None


def test_hold_gives_the_lease_back_when_its_command_fails(lease_path: Path) -> None:
    clock = _TickingClock(_TAKEN_AT)

    exit_code = hold_lease(
        lease_path,
        "restore",
        ["sh", "-c", "exit 7"],
        renew_interval_seconds=60.0,
        clock=clock,
        is_kept=False,
    )

    assert exit_code == 7
    assert read_lease(lease_path, clock.now) is None


def test_hold_gives_the_lease_back_when_its_command_cannot_be_run(
    lease_path: Path, tmp_path: Path
) -> None:
    clock = _TickingClock(_TAKEN_AT)

    with pytest.raises(FileNotFoundError):
        hold_lease(
            lease_path,
            "restore",
            [str(tmp_path / "no-such-program")],
            renew_interval_seconds=60.0,
            clock=clock,
            is_kept=False,
        )

    assert read_lease(lease_path, clock.now) is None


def test_hold_runs_under_a_lease_its_holder_took_and_keeps_it_when_asked(
    lease_path: Path,
) -> None:
    clock = _TickingClock(_TAKEN_AT)
    take_lease(lease_path, "restore", clock())

    exit_code = hold_lease(
        lease_path,
        "restore",
        ["true"],
        renew_interval_seconds=60.0,
        clock=clock,
        is_kept=True,
    )

    assert exit_code == 0
    kept = read_lease(lease_path, clock.now)
    assert kept is not None
    assert (kept.holder, kept.expires_at) == ("restore", clock.now + LEASE_SECONDS)


def test_hold_does_not_run_its_command_under_someone_elses_lease(
    lease_path: Path, tmp_path: Path
) -> None:
    taken = take_lease(lease_path, "setup", _TAKEN_AT)
    marker = tmp_path / "ran"

    with pytest.raises(BackupLeaseHeldError):
        hold_lease(
            lease_path,
            "restore",
            ["touch", str(marker)],
            renew_interval_seconds=60.0,
            clock=lambda: _TAKEN_AT + 1.0,
            is_kept=False,
        )

    assert not marker.exists()
    assert read_lease(lease_path, _TAKEN_AT + 1.0) == taken


def _run_program(lease_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the program as the Imbue Studio desktop does: the stub, by its path, with no site-packages."""
    return subprocess.run(
        [
            sys.executable,
            "-S",
            "-s",
            str(_STUB_PATH),
            "--lease-path",
            str(lease_path),
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_the_program_takes_reads_renews_and_gives_back(lease_path: Path) -> None:
    taken = _run_program(lease_path, "take", "--holder", "restore")
    assert taken.returncode == 0, taken.stderr
    assert json.loads(taken.stdout)["holder"] == "restore"

    refused = _run_program(lease_path, "take", "--holder", "store-migration")
    assert refused.returncode == EXIT_NOT_HELD
    assert json.loads(refused.stdout)["holder"] == "restore"
    assert "'restore'" in refused.stderr

    assert json.loads(_run_program(lease_path, "read").stdout)["holder"] == "restore"
    renewed = _run_program(lease_path, "renew", "--holder", "restore")
    assert renewed.returncode == 0, renewed.stderr
    assert (
        json.loads(renewed.stdout)["expires_at"]
        >= json.loads(taken.stdout)["expires_at"]
    )

    released = _run_program(lease_path, "release", "--holder", "restore")
    assert (released.returncode, json.loads(released.stdout)) == (0, None)
    assert json.loads(_run_program(lease_path, "read").stdout) is None
    lapsed = _run_program(lease_path, "renew", "--holder", "restore")
    assert (lapsed.returncode, json.loads(lapsed.stdout)) == (EXIT_NOT_HELD, None)


def test_the_program_holds_the_lease_around_a_command_and_answers_its_exit_code(
    lease_path: Path,
) -> None:
    held = _run_program(
        lease_path,
        "hold",
        "--holder",
        "restore",
        "--",
        sys.executable,
        "-S",
        "-s",
        str(_STUB_PATH),
        "--lease-path",
        str(lease_path),
        "read",
    )
    assert held.returncode == 0, held.stderr
    assert json.loads(held.stdout)["holder"] == "restore"
    assert json.loads(_run_program(lease_path, "read").stdout) is None

    failed = _run_program(
        lease_path, "hold", "--holder", "restore", "--", "sh", "-c", "exit 9"
    )
    assert failed.returncode == 9
    assert json.loads(_run_program(lease_path, "read").stdout) is None

    assert (
        _run_program(lease_path, "hold", "--holder", "restore").returncode
        == backup_lease.EXIT_USAGE
    )


def test_the_lease_is_kept_where_the_environment_names(
    lease_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(backup_lease.LEASE_PATH_ENV, raising=False)
    assert backup_lease.default_lease_path() == Path("/run/backup-operation.lease")

    monkeypatch.setenv(backup_lease.LEASE_PATH_ENV, str(lease_path))
    assert backup_lease.default_lease_path() == lease_path
