import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from app_install.cli import main
from app_install.errors import NoSnapshotError
from app_install.errors import NotInstalledAppError
from app_install.errors import UpdateInProgressError
from app_install.git import run_git
from app_install.installing import install_app
from app_install.mock_runtime_test import RecordingRuntime
from app_install.operations import add_app
from app_install.operations import check
from app_install.testing import commit_all
from app_install.testing import make_published_app_repository
from app_install.testing import make_workspace_repository
from app_install.testing import never_bound
from app_install.testing import write_user_app
from app_install.testing import write_workspace_file
from app_install.updating import UpdateResult
from app_install.updating import update_app
from app_install.updating import update_worktree_path
from app_manifest.records import load_record


@pytest.fixture(autouse=True)
def _no_registry_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDS_APPS_FILE", raising=False)


def _notes_py(greeting: str, limit: int) -> str:
    """The app's one module: a greeting on its first line and a limit on its last, far enough apart that a change
    to each merges."""
    middle = "".join(f"# line {number}\n" for number in range(1, 9))
    return f"GREETING = {greeting!r}\n{middle}LIMIT = {limit}\n"


def _installed_notes(tmp_path: Path) -> tuple[Path, Path]:
    """A workspace with the notes app installed at its first version; the workspace and the published repo."""
    workspace = make_workspace_repository(tmp_path / "workspace")
    published = make_published_app_repository(tmp_path / "published-notes", "notes", '\nversion = "0.1.0"\n')
    (published / "notes.py").write_text(_notes_py("hello", 10))
    (published / "uv.lock").write_text("lock of 0.1.0\n")
    commit_all(published, "notes 0.1.0")
    run_git(published, ["tag", "v0.1.0"])
    install_app(workspace, f"file://{published}", None, RecordingRuntime(repo_root=workspace), never_bound)
    return workspace, published


def _publish(published: Path, version: str, notes_py: str, lock: str) -> str:
    (published / "app.toml").write_text(
        f'name = "notes"\ndisplay_name = "Notes"\nicon = "icon.svg"\nversion = "{version}"\n'
    )
    (published / "notes.py").write_text(notes_py)
    (published / "uv.lock").write_text(lock)
    commit = commit_all(published, f"notes {version}")
    run_git(published, ["tag", f"v{version}"])
    return commit


def _app_file(workspace: Path, name: str) -> Path:
    return workspace / "system" / "apps" / "notes" / name


def test_an_update_brings_the_new_version_over_the_workspaces_own_changes(tmp_path: Path) -> None:
    workspace, published = _installed_notes(tmp_path)
    # The workspace changed one line of the app; the publisher changed another.
    _app_file(workspace, "notes.py").write_text(_notes_py("hello", 25))
    commit_all(workspace, "raise the limit")
    released = _publish(published, "0.2.0", _notes_py("good morning", 10), "lock of 0.1.0\n")
    write_workspace_file(workspace, "data/.apps/notes/notes.json", '["first"]')
    runtime = RecordingRuntime(repo_root=workspace)

    outcome = update_app(workspace, "notes", None, False, runtime, never_bound)

    assert (outcome.result, outcome.ref, outcome.previous_version) == (UpdateResult.UPDATED, "v0.2.0", "0.1.0")
    record = load_record(workspace, "notes")
    assert (record.upstream_commit, record.upstream_version, record.port) == (released, "0.2.0", 8080)
    assert _app_file(workspace, "notes.py").read_text() == _notes_py("good morning", 25)
    assert run_git(workspace, ["rev-parse", "refs/app-upstream/notes"]) == released
    assert run_git(workspace, ["status", "--porcelain"]) == ""
    assert check(workspace) == ()
    # The app's data was copied aside first, out of the backup, and the update's worktree is gone.
    snapshot = workspace / outcome.snapshot
    assert (snapshot / "notes.json").read_text() == '["first"]'
    assert (snapshot.parent / ".nobackup").is_file()
    assert not update_worktree_path(workspace, "notes").exists()
    assert runtime.calls == [
        "verify_app notes",
        "install_environment apt=[]",
        "reload_programs",
        "restart_programs ['notes']",
    ]


def test_an_app_already_at_the_newest_version_is_left_alone(tmp_path: Path) -> None:
    workspace, _ = _installed_notes(tmp_path)
    before = run_git(workspace, ["rev-parse", "HEAD"])
    runtime = RecordingRuntime(repo_root=workspace)

    outcome = update_app(workspace, "notes", None, False, runtime, never_bound)

    assert outcome.result is UpdateResult.UP_TO_DATE
    assert run_git(workspace, ["rev-parse", "HEAD"]) == before
    assert runtime.calls == []


def test_a_conflict_in_the_apps_code_waits_in_a_worktree_and_is_finished_with_continue(tmp_path: Path) -> None:
    workspace, published = _installed_notes(tmp_path)
    _app_file(workspace, "notes.py").write_text(_notes_py("hi there", 10))
    commit_all(workspace, "a friendlier greeting")
    released = _publish(published, "0.2.0", _notes_py("good morning", 10), "lock of 0.1.0\n")
    write_workspace_file(workspace, "data/.apps/notes/notes.json", '["first"]')
    before = run_git(workspace, ["rev-parse", "HEAD"])
    runtime = RecordingRuntime(repo_root=workspace)

    stopped = update_app(workspace, "notes", None, False, runtime, never_bound)

    worktree = update_worktree_path(workspace, "notes")
    assert stopped.result is UpdateResult.CONFLICT
    assert (workspace / stopped.snapshot / "notes.json").read_text() == '["first"]'
    assert stopped.conflicted_paths == ("system/apps/notes/notes.py",)
    assert stopped.worktree == "data/.state/app_install/worktrees/notes"
    # The live app is exactly as it was.
    assert run_git(workspace, ["rev-parse", "HEAD"]) == before
    assert _app_file(workspace, "notes.py").read_text() == _notes_py("hi there", 10)
    assert load_record(workspace, "notes").upstream_version == "0.1.0"
    with pytest.raises(UpdateInProgressError, match="--continue"):
        update_app(workspace, "notes", None, False, runtime, never_bound)

    # Continuing before the conflict is resolved changes nothing.
    still_stopped = update_app(workspace, "notes", None, True, runtime, never_bound)
    assert still_stopped.result is UpdateResult.CONFLICT

    # An agent resolves it in the worktree and commits there.
    (worktree / "system" / "apps" / "notes" / "notes.py").write_text(_notes_py("good morning, hi there", 10))
    run_git(worktree, ["add", "--all"])
    run_git(worktree, ["commit", "--quiet", "--no-edit"])
    finished = update_app(workspace, "notes", None, True, runtime, never_bound)

    assert finished.result is UpdateResult.UPDATED
    # The finished update still names the copy of the data made when it started.
    assert finished.snapshot == stopped.snapshot
    assert _app_file(workspace, "notes.py").read_text() == _notes_py("good morning, hi there", 10)
    assert load_record(workspace, "notes").upstream_commit == released
    assert not worktree.exists()
    assert check(workspace) == ()


def test_a_conflict_in_the_lock_alone_takes_the_publishers_and_makes_it_again(tmp_path: Path) -> None:
    workspace, published = _installed_notes(tmp_path)
    # The workspace's update pass relocked the app; the publisher relocked it too.
    _app_file(workspace, "uv.lock").write_text("lock the workspace made\n")
    commit_all(workspace, "upgrade the app's lock")
    _publish(published, "0.2.0", _notes_py("hello", 10), "lock of 0.2.0\n")
    runtime = RecordingRuntime(repo_root=workspace, relocked_lock_text="lock made again from 0.2.0\n")

    outcome = update_app(workspace, "notes", None, False, runtime, never_bound)

    assert outcome.result is UpdateResult.UPDATED
    assert _app_file(workspace, "uv.lock").read_text() == "lock made again from 0.2.0\n"
    assert runtime.calls[0] == "relock_app notes"


def test_an_update_whose_checks_fail_waits_in_its_worktree_with_the_live_app_untouched(tmp_path: Path) -> None:
    workspace, published = _installed_notes(tmp_path)
    _publish(published, "0.2.0", _notes_py("good morning", 10), "lock of 0.2.0\n")
    before = run_git(workspace, ["rev-parse", "HEAD"])
    failing = RecordingRuntime(repo_root=workspace, verify_failure="1 failed: test_greeting")

    stopped = update_app(workspace, "notes", None, False, failing, never_bound)

    assert (stopped.result, stopped.detail) == (UpdateResult.TESTS_FAILED, "1 failed: test_greeting")
    assert run_git(workspace, ["rev-parse", "HEAD"]) == before
    assert update_worktree_path(workspace, "notes").is_dir()

    # Once the checks pass, the same update is finished from where it stopped.
    finished = update_app(workspace, "notes", None, True, RecordingRuntime(repo_root=workspace), never_bound)

    assert finished.result is UpdateResult.UPDATED
    assert load_record(workspace, "notes").upstream_version == "0.2.0"


def test_only_the_newest_snapshot_of_an_apps_data_is_kept(tmp_path: Path) -> None:
    workspace, published = _installed_notes(tmp_path)
    write_workspace_file(workspace, "data/.apps/notes/notes.json", '["first"]')
    _publish(published, "0.2.0", _notes_py("hello", 11), "lock of 0.1.0\n")
    first = update_app(workspace, "notes", None, False, RecordingRuntime(repo_root=workspace), never_bound)
    write_workspace_file(workspace, "data/.apps/notes/notes.json", '["first", "second"]')
    _publish(published, "0.3.0", _notes_py("hello", 12), "lock of 0.1.0\n")

    second = update_app(workspace, "notes", None, False, RecordingRuntime(repo_root=workspace), never_bound)

    assert first.snapshot != second.snapshot
    assert not (workspace / first.snapshot).exists()
    assert (workspace / second.snapshot / "notes.json").read_text() == '["first", "second"]'


def test_an_app_with_no_stored_data_updates_without_a_snapshot(tmp_path: Path) -> None:
    workspace, published = _installed_notes(tmp_path)
    _publish(published, "0.2.0", _notes_py("hello", 11), "lock of 0.1.0\n")

    outcome = update_app(workspace, "notes", None, False, RecordingRuntime(repo_root=workspace), never_bound)

    assert (outcome.result, outcome.snapshot) == (UpdateResult.UPDATED, "")


def test_an_app_built_here_has_nothing_to_update_from(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    add_app(workspace, write_user_app(workspace, "digest"), None, never_bound)

    with pytest.raises(NotInstalledAppError, match="built in this workspace"):
        update_app(workspace, "digest", None, False, RecordingRuntime(repo_root=workspace), never_bound)


def test_continuing_when_no_update_is_waiting_is_refused(tmp_path: Path) -> None:
    workspace, _ = _installed_notes(tmp_path)

    with pytest.raises(UpdateInProgressError, match="no update of notes is waiting"):
        update_app(workspace, "notes", None, True, RecordingRuntime(repo_root=workspace), never_bound)


def test_data_that_does_not_fit_beside_itself_needs_a_recent_backup_before_an_update(tmp_path: Path) -> None:
    workspace, published = _installed_notes(tmp_path)
    write_workspace_file(workspace, "data/.apps/notes/notes.json", '["first"]')
    _publish(published, "0.2.0", _notes_py("hello", 11), "lock of 0.1.0\n")
    before = run_git(workspace, ["rev-parse", "HEAD"])
    full_disk = RecordingRuntime(repo_root=workspace, free_bytes=0)

    with pytest.raises(NoSnapshotError, match="no recent backup"):
        update_app(workspace, "notes", None, False, full_disk, never_bound)

    assert run_git(workspace, ["rev-parse", "HEAD"]) == before
    assert not update_worktree_path(workspace, "notes").exists()

    # With a recent backup to fall back on, the update goes ahead without a copy.
    backed_up = RecordingRuntime(repo_root=workspace, free_bytes=0, is_backup_recent=True)
    outcome = update_app(workspace, "notes", None, False, backed_up, never_bound)

    assert (outcome.result, outcome.snapshot) == (UpdateResult.UPDATED, "")


def test_the_command_reports_an_update_and_an_update_that_stopped(tmp_path: Path) -> None:
    workspace, published = _installed_notes(tmp_path)
    _app_file(workspace, "notes.py").write_text(_notes_py("hi there", 10))
    commit_all(workspace, "a friendlier greeting")
    _publish(published, "0.2.0", _notes_py("good morning", 10), "lock of 0.1.0\n")
    runner = CliRunner()
    root = ["--repo-root", str(workspace)]

    stopped = runner.invoke(main, [*root, "update", "notes"])
    waiting = runner.invoke(main, [*root, "update", "notes"])

    report = json.loads(stopped.output)
    assert (stopped.exit_code, report["result"]) == (1, "conflict")
    assert report["conflicted_paths"] == ["system/apps/notes/notes.py"]
    assert report["worktree"] == "data/.state/app_install/worktrees/notes"
    assert (waiting.exit_code, json.loads(waiting.output)["result"]) == (1, "refused")
    assert "--continue" in json.loads(waiting.output)["detail"]
