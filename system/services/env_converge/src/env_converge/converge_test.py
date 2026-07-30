import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from env_converge.converge import (
    OverlayEntryError,
    apply_overlay_entry,
    read_overlay_paths,
    read_pinned_snapshot_timestamp,
)
from env_converge.data_types import AptState, BaseIdentity, CargoState
from env_converge.record import (
    is_rootfs_stamped,
    read_apt_state,
    read_base_identity,
    read_cargo_state,
    stamp_rootfs,
    write_apt_state,
    write_base_identity,
    write_cargo_state,
)
from env_converge.upgrade import compute_version_deltas


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Record round-trips


def test_record_round_trips_atomically(tmp_path: Path) -> None:
    record_dir = tmp_path / "record"
    identity = BaseIdentity(
        snapshot_timestamp="20260720T000000Z",
        architecture="amd64",
        template_commit=None,
        recorded_at=_now(),
    )
    write_base_identity(record_dir, identity)
    reloaded = read_base_identity(record_dir)
    assert reloaded is not None
    assert reloaded.snapshot_timestamp == "20260720T000000Z"

    apt_state = AptState(
        manual_packages=("curl", "git"),
        version_by_package={"curl": "8.0", "git": "2.45"},
        recorded_at=_now(),
    )
    write_apt_state(record_dir, apt_state)
    reloaded_apt = read_apt_state(record_dir)
    assert reloaded_apt is not None
    assert reloaded_apt.manual_packages == ("curl", "git")
    # The on-disk shape is plain jq-friendly JSON.
    raw = json.loads((record_dir / "apt.json").read_text())
    assert raw["version_by_package"]["curl"] == "8.0"


def test_cargo_record_round_trips(tmp_path: Path) -> None:
    record_dir = tmp_path / "record"
    state = CargoState(
        version_by_crate={"ripgrep": "14.1.0"},
        toolchains=("stable-x86_64-unknown-linux-gnu",),
        default_toolchain="stable-x86_64-unknown-linux-gnu",
        recorded_at=_now(),
    )
    write_cargo_state(record_dir, state)
    reloaded = read_cargo_state(record_dir)
    assert reloaded is not None
    assert reloaded.version_by_crate == {"ripgrep": "14.1.0"}
    assert reloaded.default_toolchain == "stable-x86_64-unknown-linux-gnu"
    # The on-disk shape is plain jq-friendly JSON.
    raw = json.loads((record_dir / "cargo.json").read_text())
    assert raw["version_by_crate"]["ripgrep"] == "14.1.0"


def test_read_absent_record_returns_none(tmp_path: Path) -> None:
    assert read_base_identity(tmp_path / "nowhere") is None
    assert read_apt_state(tmp_path / "nowhere") is None
    assert read_cargo_state(tmp_path / "nowhere") is None


def test_rootfs_stamp_round_trip(tmp_path: Path) -> None:
    stamp = tmp_path / "stamps" / "rootfs-id"
    assert not is_rootfs_stamped(stamp)
    stamp_rootfs(stamp)
    assert is_rootfs_stamped(stamp)
    first_id = stamp.read_text()
    # Idempotent: a second stamp keeps the first identity.
    stamp_rootfs(stamp)
    assert stamp.read_text() == first_id


# ---------------------------------------------------------------------------
# Overlay


def test_overlay_paths_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_overlay_paths(tmp_path) == []


def test_overlay_paths_rejects_relative_entries(tmp_path: Path) -> None:
    overlay_file = tmp_path / "system" / "scripts" / "env.d" / "overlay-paths.json"
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text('["relative/path"]')
    with pytest.raises(OverlayEntryError):
        read_overlay_paths(tmp_path)


def test_apply_overlay_entry_adopts_existing_content(tmp_path: Path) -> None:
    rootfs_dir = tmp_path / "var" / "lib" / "service"
    rootfs_dir.mkdir(parents=True)
    (rootfs_dir / "state.txt").write_text("precious")
    overlay_dir = tmp_path / "overlay"

    result = apply_overlay_entry(rootfs_dir, overlay_dir)

    assert result.is_adopted
    assert rootfs_dir.is_symlink()
    assert (rootfs_dir / "state.txt").read_text() == "precious"
    assert (
        overlay_dir / str(rootfs_dir).lstrip("/") / "state.txt"
    ).read_text() == "precious"


def test_apply_overlay_entry_overlay_wins_when_both_exist(tmp_path: Path) -> None:
    rootfs_dir = tmp_path / "var" / "lib" / "service"
    rootfs_dir.mkdir(parents=True)
    (rootfs_dir / "state.txt").write_text("regenerable rootfs copy")
    overlay_dir = tmp_path / "overlay"
    overlay_copy = overlay_dir / str(rootfs_dir).lstrip("/")
    overlay_copy.mkdir(parents=True)
    (overlay_copy / "state.txt").write_text("user data")

    result = apply_overlay_entry(rootfs_dir, overlay_dir)

    assert not result.is_adopted
    assert (rootfs_dir / "state.txt").read_text() == "user data"


def test_apply_overlay_entry_is_idempotent(tmp_path: Path) -> None:
    rootfs_dir = tmp_path / "var" / "lib" / "service"
    overlay_dir = tmp_path / "overlay"
    first = apply_overlay_entry(rootfs_dir, overlay_dir)
    (rootfs_dir / "state.txt").write_text("written through the symlink")
    second = apply_overlay_entry(rootfs_dir, overlay_dir)
    assert not first.is_adopted
    assert not second.is_adopted
    assert (rootfs_dir / "state.txt").read_text() == "written through the symlink"


# ---------------------------------------------------------------------------
# Pinned timestamp + upgrade deltas


def test_read_pinned_snapshot_timestamp(tmp_path: Path) -> None:
    (tmp_path / ".mngr").mkdir()
    (tmp_path / ".mngr" / "apt-snapshot-timestamp").write_text("20260720T000000Z\n")
    assert read_pinned_snapshot_timestamp(tmp_path) == "20260720T000000Z"


def test_compute_version_deltas() -> None:
    before = "curl\t7.88.1\nbash\t5.2.15\n"
    after = "curl\t8.0.0\nbash\t5.2.15\ngit\t2.45.0\n"
    deltas = compute_version_deltas(before, after)
    assert deltas == {"curl": "7.88.1 -> 8.0.0", "git": "(absent) -> 2.45.0"}
