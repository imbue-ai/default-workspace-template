"""Unit tests for host_backup.capabilities detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from host_backup.capabilities import (
    BackupCapabilities,
    SnapshotMethod,
    are_backup_root_and_host_dir_on_one_filesystem,
    detect_backup_capabilities,
)


def test_detects_outer_trigger_when_trigger_dir_exists(tmp_path: Path) -> None:
    trigger_dir = tmp_path / "mngr-snapshot"
    trigger_dir.mkdir()
    capabilities = detect_backup_capabilities(
        trigger_dir=trigger_dir, backup_root=tmp_path / "home"
    )
    assert capabilities.method == SnapshotMethod.OUTER_TRIGGER
    assert capabilities.trigger_dir == trigger_dir
    assert capabilities.snapshot_read_path == Path("/mngr-snapshots/current")
    # outer_trigger snapshots cover the whole unified volume; restic reads the
    # home/ subtree inside each snapshot.
    assert capabilities.read_subpath == "home"


def test_detects_direct_when_no_trigger_dir_and_not_btrfs(tmp_path: Path) -> None:
    # tmp_path lives on the test runner's ordinary filesystem (not btrfs), so
    # detection falls through to DIRECT and restic reads the backup root live.
    backup_root = tmp_path / "home"
    backup_root.mkdir()
    capabilities = detect_backup_capabilities(
        trigger_dir=tmp_path / "absent-trigger", backup_root=backup_root
    )
    assert capabilities.method == SnapshotMethod.DIRECT
    assert capabilities.snapshot_read_path == backup_root
    assert capabilities.trigger_dir is None


def test_trigger_dir_that_is_a_file_does_not_count(tmp_path: Path) -> None:
    trigger_path = tmp_path / "mngr-snapshot"
    trigger_path.write_text("not a directory")
    backup_root = tmp_path / "home"
    backup_root.mkdir()
    capabilities = detect_backup_capabilities(
        trigger_dir=trigger_path, backup_root=backup_root
    )
    assert capabilities.method == SnapshotMethod.DIRECT


def test_capabilities_model_defaults() -> None:
    capabilities = BackupCapabilities(method=SnapshotMethod.DIRECT)
    assert capabilities.outer_helper_timeout_seconds == 120.0
    assert capabilities.max_local_snapshots == 5


# --- backup root coverage ---


def test_a_backup_root_on_the_same_filesystem_as_the_host_dir_is_fully_persisted() -> None:
    # The provider symlinked the home tree onto the volume, so the whole tree
    # rides the same persistent storage the mngr host dir does.
    assert (
        are_backup_root_and_host_dir_on_one_filesystem(
            backup_root_filesystem_id=49, host_dir_filesystem_id=49
        )
        is True
    )


def test_a_backup_root_on_a_different_filesystem_is_only_partly_persisted() -> None:
    # The real case: /home/user is the container's own disk while ~/.mngr is a
    # symlink onto the volume, so only the host dir survives the container.
    assert (
        are_backup_root_and_host_dir_on_one_filesystem(
            backup_root_filesystem_id=50, host_dir_filesystem_id=49
        )
        is False
    )


def test_an_unreadable_probe_does_not_raise_a_false_alarm() -> None:
    # A probe that failed is not evidence of a gap, and crying wolf here would
    # teach people to ignore the real thing.
    assert (
        are_backup_root_and_host_dir_on_one_filesystem(
            backup_root_filesystem_id=None, host_dir_filesystem_id=49
        )
        is True
    )
    assert (
        are_backup_root_and_host_dir_on_one_filesystem(
            backup_root_filesystem_id=50, host_dir_filesystem_id=None
        )
        is True
    )


def test_detection_reports_full_coverage_when_the_host_dir_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Run outside a provisioned agent host there is nothing to compare against.
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    backup_root = tmp_path / "home"
    backup_root.mkdir()
    capabilities = detect_backup_capabilities(
        trigger_dir=tmp_path / "absent-trigger", backup_root=backup_root
    )
    assert capabilities.is_backup_root_fully_persisted is True


def test_direct_never_reports_partial_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # direct takes no snapshot -- restic reads the backup root live, so nothing
    # under it is excluded even when the host dir sits elsewhere.
    host_dir = tmp_path / "elsewhere"
    host_dir.mkdir()
    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))
    backup_root = tmp_path / "home"
    backup_root.mkdir()
    capabilities = detect_backup_capabilities(
        trigger_dir=tmp_path / "absent-trigger", backup_root=backup_root
    )
    assert capabilities.method == SnapshotMethod.DIRECT
    assert capabilities.is_backup_root_fully_persisted is True
