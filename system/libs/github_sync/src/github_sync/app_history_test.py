"""What the workspace history says about an app, against real local repositories."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from app_manifest.records import app_directory_prefix, write_record
from imbue.imbue_common.model_update import to_update

from github_sync.app_history import (
    AppHistoryError,
    GitSubtreeMissingError,
    WorkspaceHistoryInSplitError,
    app_tree,
    is_app_linked,
    is_app_modified,
    is_primary_checkout,
    require_git_subtree,
    split_app,
)
from github_sync.testing import (
    add_owned_app,
    commit_all,
    git_output,
    init_published_app,
    init_workspace,
    install_app,
    link_app,
    run_git,
    write_app_file,
)

pytestmark = pytest.mark.usefixtures("isolated_git_and_gateway_env")


def test_an_owned_app_is_linked_once_its_first_split_is_rejoined(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    notes = add_owned_app(workspace, "notes")
    shelf = add_owned_app(workspace, "shelf")
    assert not is_app_linked(workspace, notes)

    link_app(workspace, notes)

    assert is_app_linked(workspace, notes)
    # A marker is for one directory: the other app is no more linked than before.
    assert not is_app_linked(workspace, shelf)


def test_an_installed_app_is_linked_from_the_start(tmp_path: Path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    published = init_published_app(tmp_path / "published", "notes")

    record = install_app(workspace, published, "notes")

    assert is_app_linked(workspace, record)


def test_an_installed_app_is_modified_only_by_a_difference_outside_the_regenerable_set(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    published = init_published_app(tmp_path / "published", "notes")
    record = install_app(workspace, published, "notes")
    assert not is_app_modified(workspace, record)

    write_app_file(workspace, record, "uv.lock", "relocked in this workspace\n")
    commit_all(workspace, "Upgrade the SDK in the app's lock")
    assert not is_app_modified(workspace, record)

    write_app_file(workspace, record, "notes.py", "GREETING = 'good morning'\n")
    commit_all(workspace, "Change the greeting")
    assert is_app_modified(workspace, record)


def test_an_owned_app_is_never_modified_and_an_uncommitted_edit_modifies_nothing(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    owned = add_owned_app(workspace, "shelf")
    published = init_published_app(tmp_path / "published", "notes")
    installed = install_app(workspace, published, "notes")

    write_app_file(workspace, owned, "shelf.py", "BOOKS = []\n")
    write_app_file(workspace, installed, "notes.py", "GREETING = 'not committed'\n")

    assert not is_app_modified(workspace, owned)
    assert not is_app_modified(workspace, installed)


def test_an_app_whose_installed_tree_is_gone_is_taken_as_modified(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    published = init_published_app(tmp_path / "published", "notes")
    record = install_app(workspace, published, "notes")
    # A tree no object of the repository answers to: the record came with a
    # template, without the history the app was installed through.
    unknown_tree = record.model_copy_update(
        to_update(
            record.field_ref().upstream_tree, "0123456789abcdef0123456789abcdef01234567"
        )
    )
    write_record(workspace, unknown_tree)

    assert is_app_modified(workspace, unknown_tree)


def test_a_split_is_the_same_commit_again_and_a_longer_history_continues_it(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    record = add_owned_app(workspace, "notes")
    head_before = git_output(workspace, "rev-parse", "HEAD")

    first = split_app(workspace, record)

    assert split_app(workspace, record) == first
    # The split is the app's directory as a repository of its own.
    assert git_output(workspace, "rev-parse", f"{first}^{{tree}}") == app_tree(
        workspace, record
    )
    assert git_output(workspace, "rev-parse", "HEAD") == head_before
    assert git_output(workspace, "status", "--porcelain") == ""

    (workspace / "README.md").write_text("a workspace, edited\n")
    commit_all(workspace, "Something that is not the app")
    assert split_app(workspace, record) == first

    write_app_file(workspace, record, "notes.py", "GREETING = 'hello'\n")
    commit_all(workspace, "Greet")
    second = split_app(workspace, record)
    assert second != first
    run_git(workspace, "merge-base", "--is-ancestor", first, second)


def test_a_directory_removed_and_made_again_is_not_split_into_the_workspaces_own_history(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    initial = git_output(workspace, "rev-parse", "HEAD")
    record = add_owned_app(workspace, "notes")
    shutil.rmtree(workspace / app_directory_prefix(record))
    commit_all(workspace, "Remove the notes app")
    write_app_file(workspace, record, "app.toml", 'name = "notes"\n')
    commit_all(workspace, "Build the notes app again")

    # git subtree keeps the commit that removed the directory as it is, with
    # everything the workspace held before it.
    raw_split = git_output(
        workspace, "subtree", "split", "--quiet", f"--prefix={app_directory_prefix(record)}"
    )
    run_git(workspace, "merge-base", "--is-ancestor", initial, raw_split)

    with pytest.raises(WorkspaceHistoryInSplitError, match="removed and made again"):
        split_app(workspace, record)


def test_a_split_leaves_no_scratch_behind_and_keeps_that_of_a_split_still_running(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    record = add_owned_app(workspace, "notes")
    cache_root = workspace / ".git" / "subtree-cache"
    # git subtree names its scratch directory by its process id: one of a
    # process still running, and one a process that has ended left behind.
    (cache_root / str(os.getpid())).mkdir(parents=True)
    ended = subprocess.Popen(["git", "--version"], stdout=subprocess.DEVNULL)
    ended.wait()
    (cache_root / str(ended.pid)).mkdir()

    split_app(workspace, record)
    split_app(workspace, record)

    assert [entry.name for entry in cache_root.iterdir()] == [str(os.getpid())]


def test_an_app_no_commit_holds_has_no_tree_and_does_not_split(tmp_path: Path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    record = add_owned_app(workspace, "notes")
    never_committed = record.model_copy_update(
        to_update(record.field_ref().package, "elsewhere")
    )

    assert app_tree(workspace, never_committed) is None
    with pytest.raises(AppHistoryError):
        split_app(workspace, never_committed)


def test_only_the_root_of_the_primary_checkout_is_one(tmp_path: Path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    worktree = tmp_path / "worker"
    run_git(workspace, "worktree", "add", "-q", "--detach", str(worktree))
    (workspace / "system").mkdir()

    assert is_primary_checkout(workspace)
    assert not is_primary_checkout(worktree)
    assert not is_primary_checkout(workspace / "system")


def test_a_git_without_subtree_is_named_rather_than_left_to_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    record = add_owned_app(workspace, "notes")
    # A git whose command directory holds no git-subtree, as some distributions ship it.
    bare_exec_path = tmp_path / "git-core"
    bare_exec_path.mkdir()
    real_exec_path = Path(git_output(workspace, "--exec-path"))
    for command in real_exec_path.iterdir():
        if command.name != "git-subtree":
            os.symlink(command, bare_exec_path / command.name)
    monkeypatch.setenv("GIT_EXEC_PATH", str(bare_exec_path))

    with pytest.raises(
        GitSubtreeMissingError, match="`git subtree` command is not installed"
    ):
        require_git_subtree(workspace)
    with pytest.raises(GitSubtreeMissingError):
        split_app(workspace, record)
