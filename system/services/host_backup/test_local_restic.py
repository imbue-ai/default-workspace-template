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
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from host_backup.capabilities import BackupCapabilities, SnapshotMethod
from host_backup.config import BackupConfig, RetentionSettings
from host_backup.restic import (
    backup as restic_backup,
)
from host_backup.restic import (
    extract_snapshot_id_from_backup_output,
    find_backup_summary,
    init_repo,
    is_repo_locked_error,
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
from host_backup.runner import _age_out_restore_markers, _LoopState, _run_forget


def _restic_available() -> bool:
    return shutil.which("restic") is not None


# The heaviest tests here drive several real restic subprocesses (init + backup +
# forget + prune) and run right up against the repo-global 10s pytest timeout, so
# the module carries its own. Still far below restic.py's own 3600s ceiling, so a
# genuinely wedged restic is caught here rather than hanging the suite.
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


def test_default_excludes_drop_app_data_copies_but_keep_what_they_copy(
    tmp_path: Path,
) -> None:
    """The default excludes drop copies of app data, never the data or an instance's state."""
    repo_dir = tmp_path / "repo"
    home = tmp_path / "home"
    data = home / "workspace" / "data"
    kept = [
        data / ".apps" / "pr-review" / "repos" / "pack.bin",
        data / ".state" / "isolated-instances" / "pr-review-test" / "instance.json",
        data / ".state" / "isolated-instances" / "pr-review-test" / "instance.log",
    ]
    dropped = [
        data
        / ".state"
        / "isolated-instances"
        / "pr-review-test"
        / "copies"
        / "data"
        / "repos"
        / "pack.bin",
        data / ".state" / "isolated-instances" / "pr-review-test" / "scratch" / "x.db",
    ]
    for path in kept + dropped:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name)

    env = _env_for_local_repo(repo_dir)
    assert init_repo(env).returncode == 0
    backup_result = restic_backup(
        source_path=home,
        excludes=BackupConfig().excludes,
        tag="default-excludes",
        env_overrides=env,
    )
    assert backup_result.returncode == 0, backup_result.stderr

    listing = run_restic(("ls", "latest"), env_overrides=env)
    assert listing.returncode == 0, listing.stderr
    # The snapshot holds the backed-up tree at its root.
    listed = set(listing.stdout.splitlines())
    for path in kept:
        assert f"/{path.relative_to(home)}" in listed, path
    for path in dropped:
        assert f"/{path.relative_to(home)}" not in listed, path


def _state_recording_events(tmp_path: Path) -> _LoopState:
    state = _LoopState(BackupCapabilities(method=SnapshotMethod.DIRECT))
    state.events_dir = tmp_path / "events"
    state.current_tick_id = "tick-integration"
    return state


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


def test_forget_thins_snapshots_that_each_came_from_their_own_snapshot_path(
    tmp_path: Path,
) -> None:
    """Retention must thin a repository whose snapshots all have distinct paths.

    This is the shape every outer_trigger workspace produces: each tick reads
    from `<mount>/snapshots/<timestamp>/home`, so no two snapshots share a path.
    Under restic's default `host,paths` grouping each one lands in a group of
    its own and "keep one hourly" keeps all of them.
    """
    repo_dir = tmp_path / "repo"
    env = _env_for_local_repo(repo_dir)
    assert init_repo(env).returncode == 0

    # Four ticks, four uniquely-named snapshot dirs, all within the same hour.
    per_tick_ids: list[str] = []
    for tick in range(4):
        source_dir = tmp_path / "snapshots" / f"2026-09-0{tick + 1}" / "home"
        source_dir.mkdir(parents=True)
        (source_dir / "f.txt").write_text(f"tick-{tick}")
        result = restic_backup(
            source_path=source_dir,
            excludes=(),
            tag=f"2026-hourly-{tick}",
            env_overrides=env,
        )
        assert result.returncode == 0, result.stderr
        per_tick_ids.append(extract_snapshot_id_from_backup_output(result.stdout))

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
    assert surviving == {per_tick_ids[-1]}, (
        "keep-hourly=1 must keep exactly the newest tick's snapshot across all "
        f"snapshot paths, but {len(surviving)} of 4 survived"
    )


def test_backup_skips_unchanged_files_when_each_tick_reads_a_new_snapshot_path(
    tmp_path: Path,
) -> None:
    """A tick reading a new snapshot path must still reuse the previous backup's file metadata.

    outer_trigger reads every tick from `<mount>/snapshots/<timestamp>/home`.
    The copy below keeps each file's size and mtime but not its inode, as a
    fresh snapshot seen through another path may not.
    """
    repo_dir = tmp_path / "repo"
    env = _env_for_local_repo(repo_dir)
    assert init_repo(env).returncode == 0

    first_tick = tmp_path / "snapshots" / "2026-09-24T10:00:00.000000Z" / "home"
    (first_tick / "workspace").mkdir(parents=True)
    for index in range(20):
        (first_tick / "workspace" / f"file-{index}.txt").write_text(f"content {index}")
    first = restic_backup(
        source_path=first_tick, excludes=(), tag="tick-1", env_overrides=env
    )
    assert first.returncode == 0, first.stderr

    second_tick = tmp_path / "snapshots" / "2026-09-24T11:00:00.000000Z" / "home"
    shutil.copytree(first_tick, second_tick)
    (second_tick / "workspace" / "file-7.txt").write_text("changed since tick 1")
    second = restic_backup(
        source_path=second_tick, excludes=(), tag="tick-2", env_overrides=env
    )
    assert second.returncode == 0, second.stderr

    summary = find_backup_summary(second.stdout)
    assert summary is not None, second.stdout
    assert summary["files_unmodified"] == 19
    assert summary["files_changed"] == 1
    assert summary["files_new"] == 0


def test_backup_stores_the_source_tree_at_the_snapshot_root(tmp_path: Path) -> None:
    """`<snapshot>:/` restores exactly the backed-up tree -- the subpath minds restores from."""
    repo_dir = tmp_path / "repo"
    env = _env_for_local_repo(repo_dir)
    assert init_repo(env).returncode == 0
    source_dir = tmp_path / "snapshots" / "2026-09-24T10:00:00.000000Z" / "home"
    (source_dir / "workspace").mkdir(parents=True)
    (source_dir / "workspace" / "notes.md").write_text("restore me")

    backup_result = restic_backup(
        source_path=source_dir, excludes=(), tag="tick", env_overrides=env
    )
    assert backup_result.returncode == 0, backup_result.stderr
    snapshot_id = extract_snapshot_id_from_backup_output(backup_result.stdout)

    target_dir = tmp_path / "restored"
    restore_result = run_restic(
        ("restore", f"{snapshot_id}:/", "--target", str(target_dir)),
        env_overrides=env,
    )
    assert restore_result.returncode == 0, restore_result.stderr
    assert sorted(
        p.relative_to(target_dir).as_posix() for p in target_dir.rglob("*")
    ) == [
        "workspace",
        "workspace/notes.md",
    ]
    assert (target_dir / "workspace" / "notes.md").read_text() == "restore me"


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

    state = _state_recording_events(tmp_path)
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


def _leave_a_dead_backups_lock(tmp_path: Path, env: dict[str, str]) -> None:
    """Leave the non-exclusive lock of a `restic backup` that was killed mid-run.

    restic takes the backup's lock before it starts the `--stdin-from-command`
    child, so the child writing to `locked` means the lock is on disk; the child
    then blocks on `hold` until it is released.
    """
    locked = tmp_path / "locked.fifo"
    hold = tmp_path / "hold.fifo"
    os.mkfifo(locked)
    os.mkfifo(hold)
    backup = subprocess.Popen(
        [
            "restic",
            "backup",
            "--stdin-from-command",
            "--",
            "sh",
            "-c",
            'echo locked > "$1"; cat "$2"',
            "sh",
            str(locked),
            str(hold),
        ],
        env={**os.environ, **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert locked.read_text().strip() == "locked"
    finally:
        backup.kill()
        # Reaped, so restic sees the lock's PID as gone rather than a zombie.
        backup.wait()
    # Opening and closing the write end gives the orphaned `cat` its EOF.
    hold.write_text("")


def test_forget_clears_a_dead_backups_lock_and_applies_retention(
    tmp_path: Path,
) -> None:
    """A killed backup's lock blocks forget until `_run_forget` unlocks and retries.

    forget needs an exclusive lock, which restic refuses while any other lock
    exists, stale or not; a new backup (non-exclusive) never trips over it, so
    nothing but forget's own retry clears it.
    """
    # Without the flag (restic < 0.17) the backup exits at once and the FIFO read
    # in _leave_a_dead_backups_lock would block until the timeout.
    backup_help = subprocess.run(
        ["restic", "backup", "--help"], capture_output=True, text=True, check=False
    )
    if "--stdin-from-command" not in backup_help.stdout:
        pytest.skip("restic backup has no --stdin-from-command (needs restic >= 0.17)")
    repo_dir = tmp_path / "repo"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    env = _env_for_local_repo(repo_dir)
    assert init_repo(env).returncode == 0
    # Two snapshots, so keep-*=1 has one to forget once the lock is cleared.
    snapshot_ids: list[str] = []
    for version in ("v1", "v2"):
        (source_dir / "f.txt").write_text(version)
        result = restic_backup(
            source_path=source_dir, excludes=(), tag=version, env_overrides=env
        )
        assert result.returncode == 0, result.stderr
        snapshot_ids.append(extract_snapshot_id_from_backup_output(result.stdout))

    _leave_a_dead_backups_lock(tmp_path, env)
    assert list((repo_dir / "locks").iterdir()), "the killed backup left no lock"
    blocked = restic_forget(
        keep_hourly=1, keep_daily=1, keep_weekly=1, keep_monthly=1, env_overrides=env
    )
    assert blocked.returncode != 0
    assert is_repo_locked_error(blocked.stderr), blocked.stderr

    state = _state_recording_events(tmp_path)
    _run_forget(
        state=state,
        config=BackupConfig(
            retention=RetentionSettings(
                keep_hourly=1, keep_daily=1, keep_weekly=1, keep_monthly=1
            )
        ),
        env_overrides=env,
    )

    events = [
        json.loads(line)
        for line in (state.events_dir / "events.jsonl").read_text().splitlines()
    ]
    forget_events = [e for e in events if e["type"] == "FORGET_COMPLETED"]
    assert [e["exit_code"] for e in forget_events] == [0], forget_events
    assert _snapshot_ids(env) == {snapshot_ids[-1]}
    assert list((repo_dir / "locks").iterdir()) == []
