import json
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from env_converge.converge import OverlayEntryError
from env_converge.converge import _install_missing
from env_converge.converge import _merge_missing_recorded_apt
from env_converge.converge import _merge_missing_recorded_cargo
from env_converge.converge import _merge_missing_recorded_npm
from env_converge.converge import _merge_missing_recorded_uv
from env_converge.converge import apply_overlay_entry
from env_converge.converge import capture_unless_fresh_rootfs
from env_converge.converge import read_overlay_paths
from env_converge.converge import read_pinned_snapshot_timestamp
from env_converge.data_types import AptState
from env_converge.data_types import BaseIdentity
from env_converge.data_types import CargoState
from env_converge.data_types import NpmGlobalState
from env_converge.data_types import UvToolState
from env_converge.events import default_events_path
from env_converge.record import is_rootfs_stamped
from env_converge.record import read_apt_state
from env_converge.record import read_base_identity
from env_converge.record import read_cargo_state
from env_converge.record import read_record_snapshot
from env_converge.record import stamp_rootfs
from env_converge.record import write_apt_state
from env_converge.record import write_base_identity
from env_converge.record import write_cargo_state
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


# ---------------------------------------------------------------------------
# Fresh-rootfs capture guard (the record must survive to be replayed)


def test_capture_skips_on_unstamped_rootfs_and_preserves_the_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression for the fresh-rootfs record clobber: on a rootfs without the
    identity stamp, a capture (e.g. the apt Post-Invoke hook firing off a unit's
    install) must leave the record untouched so the replay still sees it."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))
    record_dir = tmp_path / "record"
    recorded = AptState(
        manual_packages=("cowsay", "user-pkg-71c3"),
        version_by_package={"cowsay": "3.03", "user-pkg-71c3": "1.0"},
        recorded_at=_now(),
    )
    write_apt_state(record_dir, recorded)
    original_apt_json = (record_dir / "apt.json").read_text()

    is_captured = capture_unless_fresh_rootfs(
        record_dir,
        tmp_path / "workspace",
        is_forced=False,
        stamp_path=tmp_path / "stamps" / "rootfs-id",
    )

    assert not is_captured
    assert (record_dir / "apt.json").read_text() == original_apt_json
    assert not (record_dir / "npm.json").exists()
    events_path = default_events_path()
    assert events_path is not None
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert [event["type"] for event in events] == ["capture_skipped_fresh_rootfs"]
    assert events[0]["detail"] == {"record_dir": str(record_dir)}


def test_read_record_snapshot_reads_present_sources_and_leaves_absent_none(
    tmp_path: Path,
) -> None:
    record_dir = tmp_path / "record"
    write_apt_state(
        record_dir,
        AptState(
            manual_packages=("jq",),
            version_by_package={"jq": "1.7"},
            recorded_at=_now(),
        ),
    )

    snapshot = read_record_snapshot(record_dir)

    assert snapshot.apt_state is not None
    assert snapshot.apt_state.manual_packages == ("jq",)
    assert snapshot.npm_state is None
    assert snapshot.uv_tool_state is None
    assert snapshot.cargo_state is None


# ---------------------------------------------------------------------------
# Preserving recorded entries the replay could not install


def test_merge_missing_recorded_apt_keeps_uninstalled_manual_packages() -> None:
    recorded = AptState(
        manual_packages=("cowsay", "curl"),
        version_by_package={"cowsay": "3.03", "curl": "7.88"},
        recorded_at=_now(),
    )
    captured = AptState(
        manual_packages=("curl", "unzip"),
        version_by_package={"curl": "7.88", "unzip": "6.0"},
        recorded_at=_now(),
    )

    merged = _merge_missing_recorded_apt(recorded, captured)

    # cowsay failed to replay: it stays in the manual set (so a later fresh
    # boot replays it) but is NOT invented into the installed-version map.
    assert merged.manual_packages == ("cowsay", "curl", "unzip")
    assert merged.version_by_package == {"curl": "7.88", "unzip": "6.0"}
    assert merged.recorded_at == captured.recorded_at


def test_merge_missing_recorded_apt_without_recorded_state_is_captured_verbatim() -> (
    None
):
    captured = AptState(
        manual_packages=("curl",),
        version_by_package={"curl": "7.88"},
        recorded_at=_now(),
    )
    assert _merge_missing_recorded_apt(None, captured) == captured


def test_merge_missing_recorded_npm_and_uv_keep_uninstalled_entries_captured_wins() -> (
    None
):
    recorded_npm = NpmGlobalState(
        version_by_package={"left-pad-7d1e": "1.3.0", "prettier": "3.0.0"},
        recorded_at=_now(),
    )
    captured_npm = NpmGlobalState(
        version_by_package={"prettier": "3.3.0"}, recorded_at=_now()
    )
    merged_npm = _merge_missing_recorded_npm(recorded_npm, captured_npm)
    assert merged_npm.version_by_package == {
        "left-pad-7d1e": "1.3.0",
        "prettier": "3.3.0",
    }

    recorded_uv = UvToolState(version_by_tool={"ruff": "0.6.0"}, recorded_at=_now())
    captured_uv = UvToolState(version_by_tool={}, recorded_at=_now())
    merged_uv = _merge_missing_recorded_uv(recorded_uv, captured_uv)
    assert merged_uv.version_by_tool == {"ruff": "0.6.0"}


def test_merge_missing_recorded_cargo_keeps_crates_and_toolchain() -> None:
    recorded = CargoState(
        version_by_crate={"ripgrep": "14.1.0"},
        toolchains=("stable-x86_64-unknown-linux-gnu",),
        default_toolchain="stable-x86_64-unknown-linux-gnu",
        recorded_at=_now(),
    )
    captured = CargoState(
        version_by_crate={},
        toolchains=(),
        default_toolchain=None,
        recorded_at=_now(),
    )

    merged = _merge_missing_recorded_cargo(recorded, captured)

    assert merged.version_by_crate == {"ripgrep": "14.1.0"}
    assert merged.toolchains == ("stable-x86_64-unknown-linux-gnu",)
    assert merged.default_toolchain == "stable-x86_64-unknown-linux-gnu"


def test_merge_missing_recorded_cargo_captured_default_toolchain_wins() -> None:
    recorded = CargoState(
        version_by_crate={},
        toolchains=("1.79-x86_64-unknown-linux-gnu",),
        default_toolchain="1.79-x86_64-unknown-linux-gnu",
        recorded_at=_now(),
    )
    captured = CargoState(
        version_by_crate={},
        toolchains=("stable-x86_64-unknown-linux-gnu",),
        default_toolchain="stable-x86_64-unknown-linux-gnu",
        recorded_at=_now(),
    )

    merged = _merge_missing_recorded_cargo(recorded, captured)

    assert merged.default_toolchain == "stable-x86_64-unknown-linux-gnu"
    assert merged.toolchains == (
        "stable-x86_64-unknown-linux-gnu",
        "1.79-x86_64-unknown-linux-gnu",
    )


# ---------------------------------------------------------------------------
# Per-entry fallback when a batched install fails


def _logging_install_command_builder(
    log_path: Path, failing_entry: str
) -> Callable[[Sequence[str]], list[str]]:
    """Build install commands that log each invocation and fail on one entry."""

    def build(entries: Sequence[str]) -> list[str]:
        script = (
            f'printf "%s\\n" "$*" >> "{log_path}"; '
            f'for entry in "$@"; do '
            f'if [ "$entry" = "{failing_entry}" ]; then exit 1; fi; '
            "done; exit 0"
        )
        return ["bash", "-c", script, "--", *entries]

    return build


def test_install_missing_batch_success_is_one_invocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))
    log_path = tmp_path / "invocations.log"
    build = _logging_install_command_builder(log_path, "never-fails-3b9d")

    installed, unavailable = _install_missing(
        "apt", ["pkg-a-52e0", "pkg-b-52e0"], build
    )

    assert installed == ["pkg-a-52e0", "pkg-b-52e0"]
    assert unavailable == []
    assert log_path.read_text().splitlines() == ["pkg-a-52e0 pkg-b-52e0"]


def test_install_missing_failed_batch_falls_back_to_per_entry_installs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))
    log_path = tmp_path / "invocations.log"
    build = _logging_install_command_builder(log_path, "bad-entry-4f21")

    installed, unavailable = _install_missing(
        "apt", ["pkg-a-9c47", "bad-entry-4f21", "pkg-b-9c47"], build
    )

    assert installed == ["pkg-a-9c47", "pkg-b-9c47"]
    assert unavailable == ["bad-entry-4f21"]
    # One failed batch, then one invocation per entry.
    assert log_path.read_text().splitlines() == [
        "pkg-a-9c47 bad-entry-4f21 pkg-b-9c47",
        "pkg-a-9c47",
        "bad-entry-4f21",
        "pkg-b-9c47",
    ]
    events_path = default_events_path()
    assert events_path is not None
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    unavailable_events = [
        event for event in events if event["type"] == "package_unavailable"
    ]
    installed_events = [
        event for event in events if event["type"] == "package_installed"
    ]
    assert [event["detail"]["packages"] for event in unavailable_events] == [
        ["bad-entry-4f21"]
    ]
    assert [event["detail"]["packages"] for event in installed_events] == [
        ["pkg-a-9c47", "pkg-b-9c47"]
    ]


def test_install_missing_single_entry_failure_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))
    log_path = tmp_path / "invocations.log"
    build = _logging_install_command_builder(log_path, "bad-entry-8a35")

    installed, unavailable = _install_missing("npm", ["bad-entry-8a35"], build)

    assert installed == []
    assert unavailable == ["bad-entry-8a35"]
    assert log_path.read_text().splitlines() == ["bad-entry-8a35"]


def test_install_missing_with_nothing_missing_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))
    log_path = tmp_path / "invocations.log"
    build = _logging_install_command_builder(log_path, "unused-bc71")

    installed, unavailable = _install_missing("uv_tool", [], build)

    assert installed == []
    assert unavailable == []
    assert not log_path.exists()
