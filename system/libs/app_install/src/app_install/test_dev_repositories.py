"""An app's dev repository from end to end: linked by the installer, then filled and kept current by the sync
pass, with a local bare repository standing in for the one on GitHub."""

from pathlib import Path

from app_install.git import run_git
from app_install.installing import install_app
from app_install.linking import LinkResult
from app_install.linking import link_app
from app_install.mock_runtime_test import RecordingRuntime
from app_install.operations import add_app
from app_install.testing import commit_all
from app_install.testing import make_published_app_repository
from app_install.testing import make_workspace_repository
from app_install.testing import never_bound
from app_install.testing import write_app_file
from app_install.testing import write_user_app
from app_install.testing import write_workspace_file
from app_install.updating import UpdateResult
from app_install.updating import update_app
from app_manifest.records import load_record
from github_sync.app_sync import AppSyncReport
from github_sync.app_sync import AppSyncState
from github_sync.app_sync import VisibilityLedger
from github_sync.app_sync import pushed_state_path
from github_sync.app_sync import run_app_pass
from github_sync.testing import always_private

_DEV_REPO_URL = "https://github.com/octo/notes-dev"


def _workspace_with_sync(tmp_path: Path) -> tuple[Path, Path]:
    """A workspace with GitHub sync enabled, and the bare repository that stands in for the dev repository the
    installer's stand-in runtime says it created on GitHub."""
    workspace = make_workspace_repository(tmp_path / "workspace")
    write_workspace_file(
        workspace, "data/system/github_sync.toml", 'repo_url = "https://github.com/octo/my-workspace"\n'
    )
    dev_repository = tmp_path / "notes-dev.git"
    run_git(tmp_path, ["init", "--quiet", "--bare", "--initial-branch=main", str(dev_repository)])
    # No test reaches GitHub: the dev repository's URL resolves to the local one.
    run_git(workspace, ["config", f"url.{dev_repository}.insteadOf", _DEV_REPO_URL])
    return workspace, dev_repository


def _pass(workspace: Path) -> AppSyncReport:
    (report,) = run_app_pass(workspace, VisibilityLedger(), always_private)
    return report


def test_an_app_built_here_is_linked_then_pushed_and_each_later_push_continues_the_last(tmp_path: Path) -> None:
    workspace, dev_repository = _workspace_with_sync(tmp_path)
    add_app(workspace, write_user_app(workspace, "notes"), None, never_bound)
    commit_all(workspace, "Add the notes app")
    runtime = RecordingRuntime(repo_root=workspace, is_committing=True)

    # Before it is linked the app has no dev repository, and the pass names it rather than splitting it.
    assert _pass(workspace).state == AppSyncState.NO_DEV_REPOSITORY

    linked = link_app(workspace, "notes", False, False, runtime)
    first = _pass(workspace)

    assert (linked.result, linked.record.dev_repo_url) == (LinkResult.LINKED, _DEV_REPO_URL)
    assert (first.state, first.pushed_commit) == (AppSyncState.PUSHED, linked.split_commit)
    assert run_git(dev_repository, ["rev-parse", "refs/heads/main"]) == linked.split_commit
    # The dev repository is the app's directory and nothing of the workspace around it.
    files = run_git(dev_repository, ["ls-tree", "-r", "--name-only", "main"]).splitlines()
    assert "app.toml" in files
    assert not any(name.startswith(("system/", "README")) for name in files)

    write_app_file(workspace, "notes", "notes.py", "GREETING = 'hello'\n")
    commit_all(workspace, "Greet")
    second = _pass(workspace)
    refreshed = link_app(workspace, "notes", True, False, runtime)
    after_refresh = _pass(workspace)

    assert second.state == AppSyncState.PUSHED
    assert run_git(dev_repository, ["rev-parse", "main^1"]) == linked.split_commit
    assert run_git(dev_repository, ["log", "-1", "--format=%s", "main"]) == "Greet"
    # A new marker moves the primary branch and nothing in the app: there is nothing to push.
    assert (refreshed.result, refreshed.split_commit) == (LinkResult.REFRESHED, second.pushed_commit)
    assert (after_refresh.state, after_refresh.pushed_commit) == (AppSyncState.UP_TO_DATE, second.pushed_commit)

    # A restored workspace has lost what the pass kept: it pushes again, and the dev repository is unchanged.
    pushed_state_path(workspace, "notes").unlink()
    restored = _pass(workspace)
    assert (restored.state, restored.pushed_commit) == (AppSyncState.PUSHED, second.pushed_commit)
    assert run_git(dev_repository, ["rev-parse", "refs/heads/main"]) == second.pushed_commit


def test_a_modified_installed_apps_dev_repository_descends_from_its_upstream_through_an_update(
    tmp_path: Path,
) -> None:
    workspace, dev_repository = _workspace_with_sync(tmp_path)
    published = make_published_app_repository(tmp_path / "published-notes", "notes", '\nversion = "0.1.0"\n')
    (published / "notes.py").write_text("GREETING = 'hello'\n" + "# a line\n" * 8 + "LIMIT = 10\n")
    first_release = commit_all(published, "notes 0.1.0")
    run_git(published, ["tag", "v0.1.0"])
    runtime = RecordingRuntime(repo_root=workspace, is_committing=True)
    install_app(workspace, f"file://{published}", None, runtime, never_bound)

    # Left as it came, the app has no dev repository and the pass says so without pushing anything.
    assert _pass(workspace).state == AppSyncState.UNMODIFIED

    write_app_file(workspace, "notes", "notes.py", "GREETING = 'hello'\n" + "# a line\n" * 8 + "LIMIT = 25\n")
    commit_all(workspace, "Raise the limit")
    linked = link_app(workspace, "notes", False, False, runtime)
    pushed = _pass(workspace)

    assert (linked.result, linked.marker_commit, linked.is_dev_repository_created) == (
        LinkResult.ALREADY_LINKED,
        "",
        True,
    )
    assert pushed.state == AppSyncState.PUSHED
    # The adopter's repository shows where the app came from: the publisher's commits, then the adopter's.
    run_git(dev_repository, ["merge-base", "--is-ancestor", first_release, "main"])
    assert run_git(dev_repository, ["log", "-1", "--format=%s", "main"]) == "Raise the limit"

    # The publisher releases a new version, and the update merges it over the adopter's change.
    (published / "app.toml").write_text(
        'name = "notes"\ndisplay_name = "Notes"\nicon = "icon.svg"\nversion = "0.2.0"\n'
    )
    (published / "notes.py").write_text("GREETING = 'good morning'\n" + "# a line\n" * 8 + "LIMIT = 10\n")
    second_release = commit_all(published, "notes 0.2.0")
    run_git(published, ["tag", "v0.2.0"])
    updated = update_app(workspace, "notes", None, False, runtime)
    after_update = _pass(workspace)

    assert updated.result == UpdateResult.UPDATED
    assert load_record(workspace, "notes").dev_repo_url == _DEV_REPO_URL
    assert after_update.state == AppSyncState.PUSHED
    # The push continued what was there, and now descends from the new release as well.
    run_git(dev_repository, ["merge-base", "--is-ancestor", pushed.pushed_commit, "main"])
    run_git(dev_repository, ["merge-base", "--is-ancestor", second_release, "main"])
    assert run_git(dev_repository, ["show", "main:notes.py"]).splitlines()[0] == "GREETING = 'good morning'"
    assert run_git(dev_repository, ["show", "main:notes.py"]).splitlines()[-1] == "LIMIT = 25"
