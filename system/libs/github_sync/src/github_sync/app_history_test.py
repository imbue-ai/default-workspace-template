"""What the workspace history says about an app, against real local repositories."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from app_manifest.records import (
    AppRecord,
    app_directory_prefix,
    record_path,
    write_record,
)
from imbue.imbue_common.model_update import to_update

from github_sync.app_history import (
    AppHistoryError,
    GitSubtreeMissingError,
    SplitTimeoutError,
    WorkspaceHistoryInSplitError,
    app_tree,
    is_app_linked,
    is_app_modified,
    is_primary_checkout,
    open_app_split,
    require_git_subtree,
)
from github_sync.testing import (
    add_history,
    add_owned_app,
    commit_all,
    commit_on_branch,
    git_output,
    init_bare_repo,
    init_published_app,
    init_workspace,
    install_app,
    link_app,
    make_git_exec_path_whose_split_never_ends,
    run_git,
    split_commit,
    split_output,
    update_installed_app,
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


def _scratch_clones() -> list[Path]:
    """The scratch directories this process's splits have not removed."""
    parent = (
        Path("/var/tmp") if Path("/var/tmp").is_dir() else Path(tempfile.gettempdir())
    )
    return list(parent.glob(f"github-sync-split-{os.getpid()}-*"))


def _what_git_holds(workspace: Path) -> tuple[str, str, str, str]:
    """Everything of the workspace repository a split could write to: its refs,
    its objects, the checked-out commit, and the working tree."""
    return (
        git_output(workspace, "for-each-ref"),
        git_output(workspace, "count-objects", "-v"),
        git_output(workspace, "rev-parse", "HEAD"),
        git_output(workspace, "status", "--porcelain"),
    )


def test_a_split_is_the_same_commit_again_and_a_longer_history_continues_it(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    record = add_owned_app(workspace, "notes")
    held_before = _what_git_holds(workspace)

    with open_app_split(workspace, record) as split:
        first = split.commit
        # The split is the app's directory as a repository of its own.
        assert split.tree == app_tree(workspace, record)
        assert split_output(split, "ls-tree", "--name-only", first) == "app.toml"

    assert split_commit(workspace, record) == first
    # Nothing was written to the workspace repository, the split's commits included.
    assert _what_git_holds(workspace) == held_before
    with pytest.raises(subprocess.CalledProcessError):
        run_git(workspace, "cat-file", "-e", first)

    (workspace / "README.md").write_text("a workspace, edited\n")
    commit_all(workspace, "Something that is not the app")
    assert split_commit(workspace, record) == first

    write_app_file(workspace, record, "notes.py", "GREETING = 'hello'\n")
    commit_all(workspace, "Greet")
    with open_app_split(workspace, record) as split:
        assert split.commit != first
        assert split_output(split, "rev-parse", f"{split.commit}^1") == first


def _workspace_updated_after_its_link(tmp_path: Path) -> tuple[Path, AppRecord, str]:
    """A workspace that links an app it built, is then updated from its
    template, and changes the app: the workspace, the app, and the split its
    marker joins.

    The template has a long history, which the update brings back into reach:
    the workspace starts as a copy of the template's newest commit alone, as one
    from an older pre-baked host does, and its first update fetches the rest.
    """
    template = init_bare_repo(tmp_path / "template.git")
    add_history(template, "main", 520)
    workspace = tmp_path / "workspace"
    run_git(tmp_path, "clone", "-q", "--depth=1", f"file://{template}", str(workspace))
    run_git(workspace, "config", "user.email", "test@test.local")
    run_git(workspace, "config", "user.name", "test")
    record = add_owned_app(workspace, "notes")
    linked_split = link_app(workspace, record)
    add_history(template, "main", 3)
    run_git(workspace, "fetch", "-q", "--unshallow", "origin", "main")
    (workspace / "README.md").write_text("the workspace's own work\n")
    commit_all(workspace, "Workspace work")
    run_git(
        workspace, "merge", "-q", "--no-ff", "-m", "Update the workspace", "FETCH_HEAD"
    )
    write_app_file(workspace, record, "notes.py", "GREETING = 'hello'\n")
    commit_all(workspace, "Greet")
    return workspace, record, linked_split


def test_a_split_after_an_update_reads_only_what_follows_the_marker(
    tmp_path: Path,
) -> None:
    workspace, record, linked_split = _workspace_updated_after_its_link(tmp_path)
    assert git_output(workspace, "rev-list", "--count", "HEAD") == "529"

    with open_app_split(workspace, record) as split:
        # The clone the split reads holds the commit the marker was made on, the
        # marker with the split it joins, and the three commits made since. The
        # template's 523, which the update's merge follows, are cut.
        assert git_output(split.clone, "rev-list", "--count", "HEAD") == "6"
        assert split_output(split, "rev-parse", f"{split.commit}^1") == linked_split
        assert split_output(split, "log", "--format=%s", split.commit).splitlines() == [
            "Greet",
            "Add the notes app",
        ]
        assert split.tree == app_tree(workspace, record)


@pytest.mark.skipif(
    Path("/bin/sh").resolve().name != "dash",
    reason="only dash limits how deep a shell function may call itself",
)
def test_git_subtree_alone_cannot_split_after_an_update(tmp_path: Path) -> None:
    workspace, record, _linked_split = _workspace_updated_after_its_link(tmp_path)

    # The first commit the update brought in follows one from before the marker,
    # and git subtree goes back from there through every commit of the template.
    with pytest.raises(subprocess.CalledProcessError) as failure:
        run_git(
            workspace,
            "subtree",
            "split",
            "--quiet",
            f"--prefix={app_directory_prefix(record)}",
        )

    assert b"recursion depth" in failure.value.stderr


def test_a_cut_split_is_the_split_git_subtree_makes_of_the_whole_history(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run_git(workspace, "branch", "template")
    record = add_owned_app(workspace, "notes")
    shelf = add_owned_app(workspace, "shelf")
    link_app(workspace, record)
    # Since the marker: an update that merges commits from before it, a worker's
    # branch that left after it and changed the app, another app's marker, and
    # an app installed from its own repository.
    commit_on_branch(
        workspace, "template", "RELEASE.md", "a release\n", "A template release"
    )
    run_git(
        workspace, "merge", "-q", "--no-ff", "-m", "Update the workspace", "template"
    )
    run_git(workspace, "branch", "worker")
    write_app_file(workspace, record, "notes.py", "GREETING = 'hello'\n")
    commit_all(workspace, "Greet")
    commit_on_branch(
        workspace, "worker", "system/apps/notes/bye.py", "BYE = 'bye'\n", "Say goodbye"
    )
    run_git(workspace, "merge", "-q", "--no-ff", "-m", "Merge the worker", "worker")
    link_app(workspace, shelf)
    install_app(workspace, init_published_app(tmp_path / "published", "tasks"), "tasks")
    write_app_file(workspace, record, "notes.py", "GREETING = 'good morning'\n")
    commit_all(workspace, "Greet in the morning")

    with open_app_split(workspace, record) as split:
        cut_split = split.commit
        commits_read = git_output(split.clone, "rev-list", "--count", "HEAD")
        subjects = split_output(
            split, "log", "--topo-order", "--format=%s", cut_split
        ).splitlines()
    whole_split = git_output(
        workspace,
        "subtree",
        "split",
        "--quiet",
        "--ignore-joins",
        f"--prefix={app_directory_prefix(record)}",
    )

    # The same commit, so the same history under it, with the worker's commit
    # and its merge both in it.
    assert cut_split == whole_split
    assert subjects == [
        "Greet in the morning",
        "Merge the worker",
        "Say goodbye",
        "Greet",
        "Add the notes app",
    ]
    assert int(commits_read) < int(git_output(workspace, "rev-list", "--count", "HEAD"))


def test_a_change_merged_from_a_branch_that_left_before_the_marker_is_in_the_split(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    record = add_owned_app(workspace, "notes")
    run_git(workspace, "branch", "worker")
    write_app_file(workspace, record, "notes.py", "GREETING = 'hello'\n")
    commit_all(workspace, "Greet")
    linked_split = link_app(workspace, record)
    commit_on_branch(
        workspace, "worker", "system/apps/notes/bye.py", "BYE = 'bye'\n", "Say goodbye"
    )
    run_git(
        workspace,
        "merge",
        "-q",
        "--no-ff",
        "-m",
        "Merge the worker's goodbye",
        "worker",
    )

    with open_app_split(workspace, record) as split:
        merged = split.commit
        # The worker's commit is not one of the commits that follow the marker,
        # so its change to the app is in the split as part of the merge.
        assert split_output(split, "rev-parse", f"{merged}^@") == linked_split
        assert (
            split_output(split, "log", "-1", "--format=%s", merged)
            == "Merge the worker's goodbye"
        )
        assert split_output(split, "ls-tree", "--name-only", merged).splitlines() == [
            "app.toml",
            "bye.py",
            "notes.py",
        ]

    # A later change continues that history.
    write_app_file(workspace, record, "notes.py", "GREETING = 'good evening'\n")
    commit_all(workspace, "Greet in the evening")
    with open_app_split(workspace, record) as split:
        assert split_output(split, "rev-parse", f"{split.commit}^@") == merged


def test_an_installed_app_is_split_from_its_install_on_and_follows_its_publisher(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    add_history(workspace, "main", 40)
    run_git(workspace, "reset", "-q", "--hard")
    run_git(workspace, "branch", "template")
    published = init_published_app(tmp_path / "published", "notes")
    first_release = git_output(published, "rev-parse", "HEAD")
    record = install_app(workspace, published, "notes")
    write_app_file(workspace, record, "local.py", "LIMIT = 25\n")
    commit_all(workspace, "Add a limit")
    after_first_edit = split_commit(workspace, record)
    commit_on_branch(
        workspace, "template", "RELEASE.md", "a release\n", "A template release"
    )
    run_git(
        workspace, "merge", "-q", "--no-ff", "-m", "Update the workspace", "template"
    )
    (published / "notes.py").write_text("GREETING = 'good morning'\n")
    second_release = commit_all(published, "notes: third version")
    record = update_installed_app(workspace, published, record)
    after_update = split_commit(workspace, record)
    write_app_file(workspace, record, "local.py", "LIMIT = 50\n")
    commit_all(workspace, "Raise the limit")

    with open_app_split(workspace, record) as split:
        # The install's merge with its squash commit, the update's squash
        # commit, and the six workspace commits made since the install; none of
        # the 41 before it, nor the template's release that the update merged.
        assert git_output(split.clone, "rev-list", "--count", "HEAD") == "9"
        for earlier in (first_release, second_release, after_first_edit, after_update):
            split_output(split, "merge-base", "--is-ancestor", earlier, split.commit)
        assert split_output(
            split, "log", "--format=%s", "--topo-order", split.commit
        ).splitlines() == [
            "Raise the limit",
            "Update notes",
            "notes: third version",
            "Add a limit",
            "notes: second version",
            "notes: first version",
        ]
        assert split.tree == app_tree(workspace, record)
        cut_split = split.commit
    # The split git subtree makes by reading every commit of the workspace.
    assert cut_split == git_output(
        workspace,
        "subtree",
        "split",
        "--quiet",
        f"--prefix={app_directory_prefix(record)}",
    )


def test_an_app_installed_again_is_split_from_its_newest_install(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    published = init_published_app(tmp_path / "published", "notes")
    removed = install_app(workspace, published, "notes")
    write_app_file(workspace, removed, "local.py", "LIMIT = 25\n")
    commit_all(workspace, "Add a limit")
    shutil.rmtree(workspace / app_directory_prefix(removed))
    record_path(workspace, "notes").unlink()
    commit_all(workspace, "Uninstall notes")
    (published / "notes.py").write_text("GREETING = 'good morning'\n")
    release = commit_all(published, "notes: third version")
    record = install_app(workspace, published, "notes")
    write_app_file(workspace, record, "local.py", "LIMIT = 50\n")
    commit_all(workspace, "Add a higher limit")

    with open_app_split(workspace, record) as split:
        assert split_output(split, "rev-parse", f"{split.commit}^@") == release
        assert split_output(split, "log", "--format=%s", split.commit).splitlines() == [
            "Add a higher limit",
            "notes: third version",
            "notes: second version",
            "notes: first version",
        ]


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
        workspace,
        "subtree",
        "split",
        "--quiet",
        f"--prefix={app_directory_prefix(record)}",
    )
    run_git(workspace, "merge-base", "--is-ancestor", initial, raw_split)

    with pytest.raises(WorkspaceHistoryInSplitError, match="removed and made again"):
        split_commit(workspace, record)
    assert _scratch_clones() == []


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

    with open_app_split(workspace, record) as split:
        # The clone the split was made in is there for as long as the split is open.
        assert _scratch_clones() == [split.clone.parent]
        assert (split.object_directory / split.commit[:2] / split.commit[2:]).is_file()

    assert _scratch_clones() == []
    assert [entry.name for entry in cache_root.iterdir()] == [str(os.getpid())]


def test_the_scratch_clone_is_removed_when_what_is_done_with_the_split_fails(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    record = add_owned_app(workspace, "notes")

    with pytest.raises(subprocess.CalledProcessError):
        with open_app_split(workspace, record) as split:
            run_git(workspace, "push", "no-such-remote", split.commit)

    assert _scratch_clones() == []


def test_a_scratch_clone_left_by_a_process_that_has_ended_is_removed_by_the_next_split(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    record = add_owned_app(workspace, "notes")
    ended = subprocess.Popen(["git", "--version"], stdout=subprocess.DEVNULL)
    ended.wait()
    with open_app_split(workspace, record) as split:
        scratch_parent = split.clone.parent.parent
    abandoned = scratch_parent / f"github-sync-split-{ended.pid}-abandoned"
    (abandoned / "clone").mkdir(parents=True)

    split_commit(workspace, record)

    assert not abandoned.exists()


def test_a_split_that_runs_out_of_time_says_how_much_it_had_to_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    record = add_owned_app(workspace, "notes")
    link_app(workspace, record)
    write_app_file(workspace, record, "notes.py", "GREETING = 'hello'\n")
    commit_all(workspace, "Greet")
    monkeypatch.setenv(
        "GIT_EXEC_PATH",
        str(make_git_exec_path_whose_split_never_ends(tmp_path / "git-core")),
    )

    with pytest.raises(SplitTimeoutError) as failure:
        with open_app_split(workspace, record, timeout_seconds=0.5):
            pass

    assert "did not finish in 0.5 seconds" in str(failure.value)
    # The commit the marker was made on, the marker, and the one commit since.
    assert "the 3 workspace commits made since the app's newest join marker" in str(
        failure.value
    )
    assert "app-install link notes --refresh" in str(failure.value)
    assert _scratch_clones() == []


def test_an_app_no_commit_holds_has_no_tree_and_does_not_split(tmp_path: Path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    record = add_owned_app(workspace, "notes")
    never_committed = record.model_copy_update(
        to_update(record.field_ref().package, "elsewhere")
    )

    assert app_tree(workspace, never_committed) is None
    with pytest.raises(AppHistoryError):
        split_commit(workspace, never_committed)
    assert _scratch_clones() == []


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
        split_commit(workspace, record)
