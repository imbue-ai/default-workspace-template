"""Integration test for host_backup against a local restic repository.

Exercises the full tick loop end-to-end (snapshot in DIRECT mode +
restic init + restic backup + restic forget + restic prune) against a
local `restic` repository in a tmp dir, with no network access.

Skipped automatically when the `restic` binary is not on PATH so this
test still runs cleanly in environments that haven't installed it yet
(restic ships in the DEFAULT_WORKSPACE_TEMPLATE Dockerfile + lima provision; CI runners may
not have it).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from host_backup.capabilities import BackupCapabilities, SnapshotMethod
from host_backup.config import BackupConfig, RetentionSettings
from host_backup.restic import (
    backup as restic_backup,
)
from host_backup.restic import (
    extract_snapshot_id_from_backup_output,
    init_repo,
    is_repo_missing_error,
    probe_repo,
    run_restic,
)
from host_backup.restic import (
    forget as restic_forget,
)
from host_backup.restic import (
    prune as restic_prune,
)
from host_backup.runner import _age_out_restore_markers, _LoopState


def _restic_available() -> bool:
    return shutil.which("restic") is not None


# Every test here drives several real restic subprocesses (init + backup +
# forget + prune), which takes the better part of ten seconds -- past the
# repo-global pytest timeout. Still far below restic.py's own 3600s ceiling, so
# a genuinely wedged restic is caught here rather than hanging the suite.
_RESTIC_TEST_TIMEOUT_SECONDS = 60

pytestmark = [
    pytest.mark.skipif(
        not _restic_available(),
        reason="restic binary not on PATH (install via apt-get install restic to enable)",
    ),
    pytest.mark.timeout(_RESTIC_TEST_TIMEOUT_SECONDS),
]


def _env_for_local_repo(repo_path: Path) -> dict[str, str]:
    return {
        "RESTIC_REPOSITORY": str(repo_path),
        "RESTIC_PASSWORD": "integration-test-password",
    }


def test_probe_repo_reports_missing_before_init(tmp_path: Path) -> None:
    env = _env_for_local_repo(tmp_path / "repo")
    probe = probe_repo(env)
    assert probe.returncode != 0
    assert is_repo_missing_error(probe.stderr)


def test_init_then_probe_succeeds(tmp_path: Path) -> None:
    env = _env_for_local_repo(tmp_path / "repo")
    init = init_repo(env)
    assert init.returncode == 0, init.stderr
    probe = probe_repo(env)
    assert probe.returncode == 0


def test_full_backup_forget_prune_cycle(tmp_path: Path) -> None:
    """End-to-end: init, backup, run forget, run prune; check restic snapshots roundtrip."""
    repo_dir = tmp_path / "repo"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "hello.txt").write_text("first")
    (source_dir / "skip-me").mkdir()
    (source_dir / "skip-me" / "junk.txt").write_text("ignored")

    env = _env_for_local_repo(repo_dir)
    init = init_repo(env)
    assert init.returncode == 0, init.stderr

    backup_result = restic_backup(
        source_path=source_dir,
        excludes=("**/skip-me",),
        tag="test-tag",
        env_overrides=env,
    )
    assert backup_result.returncode == 0, backup_result.stderr
    snapshot_id = extract_snapshot_id_from_backup_output(backup_result.stdout)
    assert snapshot_id, (
        "expected to parse a snapshot id from restic backup --json output"
    )

    # Restic exposes the snapshot in `snapshots`:
    snapshots = run_restic(("snapshots", "--json"), env_overrides=env)
    assert snapshots.returncode == 0, snapshots.stderr
    assert snapshot_id in snapshots.stdout

    # Make a second backup so forget has multiple snapshots to consider.
    (source_dir / "hello.txt").write_text("second")
    second_backup = restic_backup(
        source_path=source_dir,
        excludes=("**/skip-me",),
        tag="test-tag-2",
        env_overrides=env,
    )
    assert second_backup.returncode == 0, second_backup.stderr

    forget_result = restic_forget(
        keep_hourly=1,
        keep_daily=1,
        keep_weekly=1,
        keep_monthly=1,
        env_overrides=env,
    )
    assert forget_result.returncode == 0, forget_result.stderr

    prune_result = restic_prune(env)
    assert prune_result.returncode == 0, prune_result.stderr


def test_exclude_pattern_actually_skips_files(tmp_path: Path) -> None:
    """`restic backup --exclude=<glob>` must drop matching files from the snapshot."""
    repo_dir = tmp_path / "repo"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "keep.txt").write_text("kept")
    (source_dir / ".venv").mkdir()
    (source_dir / ".venv" / "should-not-be-backed-up.txt").write_text("excluded")

    env = _env_for_local_repo(repo_dir)
    init_result = init_repo(env)
    assert init_result.returncode == 0

    backup_result = restic_backup(
        source_path=source_dir,
        excludes=("**/.venv",),
        tag="exclude-test",
        env_overrides=env,
    )
    assert backup_result.returncode == 0, backup_result.stderr

    # Listing the snapshot's files via `restic ls latest` must NOT include .venv:
    listing = run_restic(("ls", "latest"), env_overrides=env)
    assert listing.returncode == 0, listing.stderr
    assert "keep.txt" in listing.stdout
    assert ".venv" not in listing.stdout


def _snapshot_ids(env: dict[str, str]) -> set[str]:
    """Return the full ids of all snapshots currently in the repo."""
    result = run_restic(("snapshots", "--json"), env_overrides=env)
    assert result.returncode == 0, result.stderr
    return {entry["id"] for entry in json.loads(result.stdout)}


def test_forget_keeps_restore_marker_that_hourly_thinning_would_drop(
    tmp_path: Path,
) -> None:
    """`--keep-tag` protects a `restored` snapshot the keep-hourly bucket would otherwise forget."""
    repo_dir = tmp_path / "repo"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "f.txt").write_text("v1")

    env = _env_for_local_repo(repo_dir)
    assert init_repo(env).returncode == 0

    # A restore marker, then two ordinary backups -- all in the same hour, so
    # keep-hourly=1 keeps only the newest ordinary one and would forget the
    # marker if it were not tag-protected.
    marker = restic_backup(
        source_path=source_dir, excludes=(), tag="restored", env_overrides=env
    )
    marker_id = extract_snapshot_id_from_backup_output(marker.stdout)
    (source_dir / "f.txt").write_text("v2")
    ordinary_old = restic_backup(
        source_path=source_dir, excludes=(), tag="2026-hourly-a", env_overrides=env
    )
    ordinary_old_id = extract_snapshot_id_from_backup_output(ordinary_old.stdout)
    (source_dir / "f.txt").write_text("v3")
    ordinary_new = restic_backup(
        source_path=source_dir, excludes=(), tag="2026-hourly-b", env_overrides=env
    )
    ordinary_new_id = extract_snapshot_id_from_backup_output(ordinary_new.stdout)

    assert (
        restic_forget(
            keep_hourly=1,
            keep_daily=1,
            keep_weekly=1,
            keep_monthly=1,
            env_overrides=env,
        ).returncode
        == 0
    )

    surviving = _snapshot_ids(env)
    assert marker_id in surviving, (
        "the restore marker must survive keep-hourly thinning"
    )
    assert ordinary_new_id in surviving, (
        "the newest ordinary backup is the hour's representative"
    )
    assert ordinary_old_id not in surviving, (
        "the older ordinary backup is still thinned normally"
    )


def test_age_out_forgets_only_expired_restore_markers(tmp_path: Path) -> None:
    """End-to-end: `_age_out_restore_markers` forgets a backdated marker and keeps a recent one."""
    repo_dir = tmp_path / "repo"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "f.txt").write_text("data")

    env = _env_for_local_repo(repo_dir)
    assert init_repo(env).returncode == 0

    # An old restore marker (backdated 30 days via restic --time), a recent
    # restore marker, and an ordinary backup.
    old_backup = run_restic(
        (
            "backup",
            "--json",
            "--tag",
            "restored",
            "--time",
            "2000-01-01 00:00:00",
            str(source_dir),
        ),
        env_overrides=env,
    )
    assert old_backup.returncode == 0, old_backup.stderr
    old_marker_id = extract_snapshot_id_from_backup_output(old_backup.stdout)
    (source_dir / "f.txt").write_text("data2")
    recent_marker_id = extract_snapshot_id_from_backup_output(
        restic_backup(
            source_path=source_dir, excludes=(), tag="restored", env_overrides=env
        ).stdout
    )
    (source_dir / "f.txt").write_text("data3")
    ordinary_id = extract_snapshot_id_from_backup_output(
        restic_backup(
            source_path=source_dir, excludes=(), tag="2026-hourly", env_overrides=env
        ).stdout
    )

    state = _LoopState(BackupCapabilities(method=SnapshotMethod.DIRECT))
    state.events_dir = tmp_path / "events"
    state.current_tick_id = "tick-integration"
    _age_out_restore_markers(
        state=state,
        config=BackupConfig(
            retention=RetentionSettings(restore_marker_max_age_days=7.0)
        ),
        env_overrides=env,
    )

    surviving = _snapshot_ids(env)
    assert old_marker_id not in surviving, (
        "the marker older than the cutoff must be forgotten"
    )
    assert recent_marker_id in surviving, "a marker within the cutoff must be kept"
    assert ordinary_id in surviving, (
        "ordinary backups are never touched by the marker age-out"
    )
