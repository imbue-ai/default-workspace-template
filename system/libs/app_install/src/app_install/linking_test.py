import json
import os
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from app_install.cli import main
from app_install.errors import AppInstallError
from app_install.errors import GitCommandError
from app_install.errors import MissingCommandError
from app_install.errors import NotModifiedAppError
from app_install.errors import NotPrimaryCheckoutError
from app_install.git import is_app_linked
from app_install.git import run_git
from app_install.installing import install_app
from app_install.linking import LinkResult
from app_install.linking import _clone_bounded_by_app_age
from app_install.linking import _make_marker_once
from app_install.linking import link_app
from app_install.mock_runtime_test import RecordingRuntime
from app_install.operations import add_app
from app_install.testing import commit_all
from app_install.testing import commit_all_dated
from app_install.testing import make_published_app_repository
from app_install.testing import make_workspace_repository
from app_install.testing import never_bound
from app_install.testing import write_app_file
from app_install.testing import write_user_app
from app_install.testing import write_workspace_file
from app_manifest.records import AppRecord
from app_manifest.records import load_record
from github_sync.app_history import split_app
from github_sync.testing import make_git_exec_path_whose_split_leaves_its_marker

_PREFIX = "system/apps/notes"


def _workspace_with_notes(tmp_path: Path) -> Path:
    """A workspace with history before, between, and after the commits of an app it built: two commits touch
    the notes app, the second of them together with a workspace file."""
    workspace = make_workspace_repository(tmp_path / "workspace")
    write_workspace_file(workspace, "README.md", "a workspace, a day on\n")
    commit_all_dated(workspace, "Workspace work before the app", "2026-01-02T12:00:00Z")
    add_app(workspace, write_user_app(workspace, "notes"), None, never_bound)
    commit_all_dated(workspace, "Add the notes app", "2026-01-03T12:00:00Z")
    write_workspace_file(workspace, "README.md", "a workspace, two days on\n")
    commit_all_dated(workspace, "Workspace work beside the app", "2026-01-04T12:00:00Z")
    write_app_file(workspace, "notes", "notes.py", "GREETING = 'hello'\n")
    write_workspace_file(workspace, "README.md", "a workspace, three days on\n")
    commit_all_dated(workspace, "Greet, and edit the workspace too", "2026-01-05T12:00:00Z")
    return workspace


def _split_of_the_whole_history(workspace: Path, tmp_path: Path) -> str:
    """What ``git subtree split`` makes of the app when it walks every workspace commit, in a copy of the
    workspace so the workspace itself is left as it is."""
    copy = tmp_path / "copy-for-the-full-walk"
    run_git(tmp_path, ["clone", "--quiet", str(workspace), str(copy)])
    return split_app(copy, load_record(workspace, "notes"))


def _enable_sync(workspace: Path) -> None:
    write_workspace_file(
        workspace, "data/system/github_sync.toml", 'repo_url = "https://github.com/octo/my-workspace"\n'
    )


def _head(workspace: Path) -> str:
    return run_git(workspace, ["rev-parse", "HEAD"])


def test_a_link_through_a_clone_bounded_by_the_apps_age_makes_the_split_of_the_whole_history(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    expected_split = _split_of_the_whole_history(workspace, tmp_path)
    head_before = _head(workspace)
    # The user's own uncommitted work, which linking must leave exactly as it is.
    write_workspace_file(workspace, "README.md", "a workspace, being edited\n")
    write_workspace_file(workspace, "draft.txt", "not committed\n")
    runtime = RecordingRuntime(repo_root=workspace)

    outcome = link_app(workspace, "notes", False, False, runtime)

    assert (outcome.result, outcome.split_commit, outcome.is_full_clone_used) == (
        LinkResult.LINKED,
        expected_split,
        False,
    )
    # The primary branch moved to the marker, which joins the old tip and the split and changes no file.
    assert _head(workspace) == outcome.marker_commit
    assert run_git(workspace, ["symbolic-ref", "--short", "HEAD"]) == "main"
    assert run_git(workspace, ["rev-parse", "HEAD^1", "HEAD^2"]).split() == [head_before, expected_split]
    assert run_git(workspace, ["rev-parse", "HEAD^{tree}"]) == run_git(
        workspace, ["rev-parse", f"{head_before}^{{tree}}"]
    )
    assert run_git(workspace, ["log", "-1", "--format=%an|%s"]) == "app-install|app-install: link notes"
    assert run_git(workspace, ["log", "-1", "--format=%(trailers:key=git-subtree-dir,valueonly)"]) == _PREFIX
    assert run_git(workspace, ["status", "--porcelain"]).splitlines() == ["M README.md", "?? draft.txt"]
    assert (workspace / "README.md").read_text() == "a workspace, being edited\n"
    # The split is the app's own history: its two commits, with its directory at the root.
    assert run_git(workspace, ["log", "--format=%s", expected_split]).splitlines() == [
        "Greet, and edit the workspace too",
        "Add the notes app",
    ]
    assert "notes.py" in run_git(workspace, ["ls-tree", "--name-only", expected_split]).splitlines()
    assert is_app_linked(workspace, outcome.record)
    # Sync is not enabled, so no repository was asked for, and the result says what that leaves.
    assert (outcome.is_dev_repository_created, outcome.record.dev_repo_url, runtime.calls) == (False, "", [])
    assert outcome.remaining[0].startswith("GitHub sync is not enabled, so no dev repository was created")


def test_the_clone_a_link_works_in_is_cut_at_the_apps_first_commit(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    cloned = _clone_bounded_by_app_age(workspace, scratch, "main", _head(workspace), _PREFIX)

    assert cloned is not None
    clone, is_full_clone_used = cloned
    assert is_full_clone_used is False
    assert run_git(clone, ["rev-parse", "--is-shallow-repository"]) == "true"
    # The app's first commit and the two after it; the workspace's two earlier commits are behind the cut.
    assert (run_git(clone, ["rev-list", "--count", "HEAD"]), run_git(workspace, ["rev-list", "--count", "HEAD"])) == (
        "3",
        "5",
    )


def test_an_early_commit_cut_off_by_its_date_makes_the_link_use_the_whole_history(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    add_app(workspace, write_user_app(workspace, "notes"), None, never_bound)
    commit_all_dated(workspace, "Add the notes app", "2026-01-10T12:00:00Z")
    # Dated before the commit it follows: a clone cut at the app's first commit stops here, short of that commit.
    write_workspace_file(workspace, "README.md", "a workspace, with its clock set back\n")
    commit_all_dated(workspace, "Workspace work dated earlier than its parent", "2026-01-05T12:00:00Z")
    write_app_file(workspace, "notes", "notes.py", "GREETING = 'hello'\n")
    commit_all_dated(workspace, "Greet", "2026-01-12T12:00:00Z")
    expected_split = _split_of_the_whole_history(workspace, tmp_path)
    head_before = _head(workspace)

    outcome = link_app(workspace, "notes", False, False, RecordingRuntime(repo_root=workspace))

    assert (outcome.result, outcome.is_full_clone_used, outcome.split_commit) == (
        LinkResult.LINKED,
        True,
        expected_split,
    )
    # Both of the app's commits are in its history, the early one included.
    assert run_git(workspace, ["log", "--format=%s", outcome.split_commit]).splitlines() == [
        "Greet",
        "Add the notes app",
    ]
    assert run_git(workspace, ["rev-parse", "HEAD^1"]) == head_before


def test_a_split_after_more_commits_continues_the_first(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    outcome = link_app(workspace, "notes", False, False, RecordingRuntime(repo_root=workspace))
    # Nothing new in the app: a split from the marker is the split the link made.
    assert split_app(workspace, outcome.record) == outcome.split_commit

    write_workspace_file(workspace, "README.md", "a workspace, later\n")
    commit_all(workspace, "Workspace work after the link")
    write_app_file(workspace, "notes", "notes.py", "GREETING = 'good morning'\n")
    commit_all(workspace, "Greet in the morning")
    second = split_app(workspace, outcome.record)

    assert run_git(workspace, ["rev-parse", f"{second}^1"]) == outcome.split_commit
    assert run_git(workspace, ["log", "-1", "--format=%s", second]) == "Greet in the morning"
    assert run_git(workspace, ["show", f"{second}:notes.py"]) == "GREETING = 'good morning'"


def test_linking_again_makes_no_marker_and_a_refresh_makes_one_once_the_app_has_changed(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    runtime = RecordingRuntime(repo_root=workspace)
    linked = link_app(workspace, "notes", False, False, runtime)

    again = link_app(workspace, "notes", False, False, runtime)
    nothing_to_refresh = link_app(workspace, "notes", True, False, runtime)

    assert (again.result, nothing_to_refresh.result) == (LinkResult.ALREADY_LINKED, LinkResult.ALREADY_LINKED)
    assert (again.marker_commit, _head(workspace)) == ("", linked.marker_commit)

    # The workspace moves on and the app does not: there is no newer split to join, so no marker is made.
    write_workspace_file(workspace, "README.md", "a workspace, later\n")
    tip = commit_all(workspace, "Workspace work after the link")
    app_unchanged = link_app(workspace, "notes", True, False, runtime)

    assert (app_unchanged.result, _head(workspace)) == (LinkResult.ALREADY_LINKED, tip)
    assert split_app(workspace, app_unchanged.record) == linked.split_commit

    # The app changes: a refresh joins the app's new commit, which continues the first split, at the tip.
    write_app_file(workspace, "notes", "notes.py", "GREETING = 'good morning'\n")
    changed_tip = commit_all(workspace, "Greet in the morning")
    write_workspace_file(workspace, "draft.txt", "not committed\n")
    refreshed = link_app(workspace, "notes", True, False, runtime)

    assert refreshed.result == LinkResult.REFRESHED
    assert run_git(workspace, ["rev-parse", f"{refreshed.split_commit}^1"]) == linked.split_commit
    assert _head(workspace) == refreshed.marker_commit
    assert run_git(workspace, ["rev-parse", "HEAD^1", "HEAD^2"]).split() == [changed_tip, refreshed.split_commit]
    assert run_git(workspace, ["log", "-1", "--format=%(trailers:key=git-subtree-mainline,valueonly)"]) == changed_tip
    assert run_git(workspace, ["diff", "--stat", changed_tip, "HEAD"]) == ""
    assert run_git(workspace, ["status", "--porcelain"]) == "?? draft.txt"
    # A split from the new marker is the one it joins, and the next change continues it.
    assert split_app(workspace, refreshed.record) == refreshed.split_commit
    write_app_file(workspace, "notes", "notes.py", "GREETING = 'good evening'\n")
    commit_all(workspace, "Greet in the evening")
    assert run_git(workspace, ["rev-parse", f"{split_app(workspace, refreshed.record)}^1"]) == refreshed.split_commit


def test_a_refresh_whose_split_cannot_start_from_the_marker_reads_the_whole_history_instead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace_with_notes(tmp_path)
    runtime = RecordingRuntime(repo_root=workspace)
    linked = link_app(workspace, "notes", False, False, runtime)
    # A branch that left the workspace before the marker is merged after it, as an update merges the template's.
    run_git(workspace, ["branch", "upstream", f"{linked.marker_commit}~2"])
    upstream = tmp_path / "upstream"
    run_git(workspace, ["worktree", "add", "--quiet", str(upstream), "upstream"])
    write_workspace_file(upstream, "RELEASE.md", "a new release of the template\n")
    commit_all(upstream, "A template release")
    run_git(workspace, ["merge", "--quiet", "--no-ff", "-m", "Merge the new release", "upstream"])
    write_app_file(workspace, "notes", "notes.py", "GREETING = 'good morning'\n")
    changed_tip = commit_all(workspace, "Greet in the morning")
    monkeypatch.setenv(
        "GIT_EXEC_PATH", str(make_git_exec_path_whose_split_leaves_its_marker(tmp_path / "git-core"))
    )

    refreshed = link_app(workspace, "notes", True, False, runtime)

    assert refreshed.result == LinkResult.REFRESHED
    assert run_git(workspace, ["rev-parse", "HEAD^1", "HEAD^2"]).split() == [changed_tip, refreshed.split_commit]
    assert run_git(workspace, ["rev-parse", f"{refreshed.split_commit}^1"]) == linked.split_commit
    assert run_git(workspace, ["log", "--format=%s", refreshed.split_commit]).splitlines() == [
        "Greet in the morning",
        "Greet, and edit the workspace too",
        "Add the notes app",
    ]
    # With the marker past the merge, a split starts from it again and the next change continues it.
    monkeypatch.delenv("GIT_EXEC_PATH")
    assert split_app(workspace, refreshed.record) == refreshed.split_commit
    write_app_file(workspace, "notes", "notes.py", "GREETING = 'good evening'\n")
    commit_all(workspace, "Greet in the evening")
    assert run_git(workspace, ["rev-parse", f"{split_app(workspace, refreshed.record)}^1"]) == refreshed.split_commit


def test_a_merge_that_brings_in_older_commits_makes_the_link_use_the_whole_history(tmp_path: Path) -> None:
    # The notes app's commits are dated long before the shelf app's, so the marker that links notes, made after
    # the shelf app's first commit, joins a split older than the cut a clone for the shelf app is made at. git
    # cuts every parent of such a commit: in the clone the marker reads as the commit that added the shelf app,
    # and the shelf app's real first commit is behind it.
    workspace = _workspace_with_notes(tmp_path)
    add_app(workspace, write_user_app(workspace, "shelf"), None, never_bound)
    commit_all(workspace, "Add the shelf app")
    runtime = RecordingRuntime(repo_root=workspace)
    copy = tmp_path / "copy-for-the-full-walk"
    run_git(tmp_path, ["clone", "--quiet", str(workspace), str(copy)])
    expected_shelf_split = split_app(copy, load_record(workspace, "shelf"))
    notes = link_app(workspace, "notes", False, False, runtime)

    shelf = link_app(workspace, "shelf", False, False, runtime)

    assert (notes.is_full_clone_used, shelf.is_full_clone_used) == (False, True)
    assert shelf.split_commit == expected_shelf_split
    assert run_git(workspace, ["log", "--format=%s", shelf.split_commit]) == "Add the shelf app"
    # Each app splits from its own marker, with the other's in the history between.
    assert split_app(workspace, notes.record) == notes.split_commit
    assert split_app(workspace, shelf.record) == shelf.split_commit

    # Neither app has changed, so a refresh has nothing to join, whatever the other app's marker added.
    head_before = _head(workspace)
    unchanged = link_app(workspace, "notes", True, False, runtime)
    assert (unchanged.result, _head(workspace)) == (LinkResult.ALREADY_LINKED, head_before)


def test_an_app_whose_directory_was_removed_and_made_again_is_not_linked_to_the_workspaces_history(
    tmp_path: Path,
) -> None:
    # git subtree keeps the commit that removed the directory as it is, so the app's split would descend from
    # the workspace's own commits and a dev repository would be handed every file the workspace ever held.
    workspace = _workspace_with_notes(tmp_path)
    shutil.rmtree(workspace / _PREFIX)
    commit_all(workspace, "Remove the notes app's directory")
    write_user_app(workspace, "notes")
    head_before = commit_all(workspace, "Build the notes app again")
    runtime = RecordingRuntime(repo_root=workspace)
    _enable_sync(workspace)

    with pytest.raises(GitCommandError, match="removed and made again"):
        link_app(workspace, "notes", False, False, runtime)

    assert _head(workspace) == head_before
    assert not is_app_linked(workspace, load_record(workspace, "notes"))
    assert load_record(workspace, "notes").dev_repo_url == ""
    assert not any(call.startswith("create_private_repository") for call in runtime.calls)


def test_a_marker_is_not_made_on_a_commit_the_primary_branch_has_left(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    record = load_record(workspace, "notes")
    stale_head = run_git(workspace, ["rev-parse", "HEAD~1"])
    head_before = _head(workspace)

    # The branch moved between reading its tip and cloning it: nothing is made, and the caller starts again.
    assert _make_marker_once(workspace, record, "main", stale_head, True) is None
    assert _head(workspace) == head_before
    assert not is_app_linked(workspace, record)


def test_linking_is_refused_in_a_worktree_that_is_not_the_primary_checkout(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    worktree = tmp_path / "worker"
    run_git(workspace, ["worktree", "add", "--quiet", "-b", "worker", str(worktree)])
    head_before = _head(workspace)

    with pytest.raises(NotPrimaryCheckoutError, match="primary checkout"):
        link_app(worktree, "notes", False, False, RecordingRuntime(repo_root=worktree))

    assert (_head(workspace), _head(worktree)) == (head_before, head_before)


def test_an_app_no_commit_holds_cannot_be_linked(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    add_app(workspace, write_user_app(workspace, "notes"), None, never_bound)

    with pytest.raises(AppInstallError, match="no commit holds system/apps/notes yet"):
        link_app(workspace, "notes", False, False, RecordingRuntime(repo_root=workspace))


def test_with_sync_enabled_a_link_creates_the_dev_repository_and_commits_its_url_alone(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    _enable_sync(workspace)
    write_workspace_file(workspace, "draft.txt", "not committed\n")
    runtime = RecordingRuntime(repo_root=workspace, is_committing=True)

    outcome = link_app(workspace, "notes", False, False, runtime)

    assert (outcome.result, outcome.is_dev_repository_created, outcome.problems) == (LinkResult.LINKED, True, ())
    assert outcome.record.dev_repo_url == "https://github.com/octo/notes-dev"
    assert load_record(workspace, "notes") == outcome.record
    assert runtime.calls[0].startswith("create_private_repository notes-dev ")
    # One commit of the installer's, on top of the marker, holding the record and nothing else.
    assert run_git(workspace, ["rev-parse", "HEAD^"]) == outcome.marker_commit
    assert run_git(workspace, ["log", "-1", "--format=%an|%s"]) == (
        "app-install|app-install: record the dev repository of notes"
    )
    assert run_git(workspace, ["show", "--format=", "--name-only", "HEAD"]) == "system/config/apps/notes.toml"
    assert run_git(workspace, ["status", "--porcelain"]) == "?? draft.txt"
    assert outcome.remaining == (
        "nothing: the github-sync service pushes the app to its dev repository on its next pass",
    )

    again = link_app(workspace, "notes", False, False, runtime)

    assert (again.result, again.is_dev_repository_created) == (LinkResult.ALREADY_LINKED, False)
    assert len([call for call in runtime.calls if call.startswith("create_private_repository")]) == 1


def test_one_app_can_be_given_a_dev_repository_without_sync_being_enabled(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    runtime = RecordingRuntime(repo_root=workspace, is_committing=True, lockdown_problem="Server Error")

    outcome = link_app(workspace, "notes", False, True, runtime)

    assert (outcome.is_dev_repository_created, outcome.record.dev_repo_url) == (
        True,
        "https://github.com/octo/notes-dev",
    )
    assert "github-sync sync-app notes" in outcome.remaining[0]
    # The repository exists and is private, so what did not take is a follow-up, not a failure.
    assert len(outcome.problems) == 1
    assert "Server Error" in outcome.problems[0]


def test_a_dev_repository_that_cannot_be_created_is_reported_and_the_marker_stands(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    _enable_sync(workspace)
    runtime = RecordingRuntime(repo_root=workspace, repository_failure="the name is taken on this account")

    outcome = link_app(workspace, "notes", False, False, runtime)

    assert (outcome.result, outcome.is_dev_repository_created) == (LinkResult.LINKED, False)
    assert _head(workspace) == outcome.marker_commit
    assert outcome.problems == ("the dev repository notes-dev was not created: the name is taken on this account",)
    assert load_record(workspace, "notes").dev_repo_url == ""
    assert not any(call.startswith("commit_paths") for call in runtime.calls)


def _installed_notes(tmp_path: Path) -> tuple[Path, AppRecord]:
    workspace = make_workspace_repository(tmp_path / "workspace")
    published = make_published_app_repository(tmp_path / "published-notes", "notes")
    (published / "uv.lock").write_text("the publisher's lock\n")
    (published / "notes.py").write_text("GREETING = 'hello'\n")
    commit_all(published, "notes: a greeting")
    outcome = install_app(workspace, f"file://{published}", None, RecordingRuntime(repo_root=workspace), never_bound)
    _enable_sync(workspace)
    return workspace, outcome.record


def test_an_installed_app_gets_a_dev_repository_only_once_it_is_modified_and_never_a_marker(tmp_path: Path) -> None:
    workspace, _record = _installed_notes(tmp_path)
    runtime = RecordingRuntime(repo_root=workspace, is_committing=True)

    with pytest.raises(NotModifiedAppError, match="installed and is unchanged"):
        link_app(workspace, "notes", False, False, runtime)
    # A lock made again here is a function of the app's other files, and modifies nothing.
    write_app_file(workspace, "notes", "uv.lock", "relocked in this workspace\n")
    commit_all(workspace, "Upgrade the SDK in the app's lock")
    with pytest.raises(NotModifiedAppError):
        link_app(workspace, "notes", False, False, runtime)
    assert runtime.calls == []

    write_app_file(workspace, "notes", "notes.py", "GREETING = 'good morning'\n")
    modified_tip = commit_all(workspace, "Change the greeting")
    outcome = link_app(workspace, "notes", True, False, runtime)

    # The squash commit the install left is what the search for a marker finds: none is made, and only the
    # record is committed.
    assert (outcome.result, outcome.marker_commit, outcome.is_dev_repository_created) == (
        LinkResult.ALREADY_LINKED,
        "",
        True,
    )
    assert run_git(workspace, ["rev-parse", "HEAD^"]) == modified_tip
    assert load_record(workspace, "notes").dev_repo_url == "https://github.com/octo/notes-dev"


def _without_git_subtree(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point git at a command directory that holds everything but git-subtree, as some distributions ship it."""
    exec_path = tmp_path / "git-core"
    exec_path.mkdir()
    for command in Path(run_git(workspace, ["--exec-path"])).iterdir():
        if command.name != "git-subtree":
            os.symlink(command, exec_path / command.name)
    monkeypatch.setenv("GIT_EXEC_PATH", str(exec_path))


def test_a_git_without_subtree_is_named_before_anything_is_tried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace_with_notes(tmp_path)
    published = make_published_app_repository(tmp_path / "published-shelf", "shelf")
    head_before = _head(workspace)
    _without_git_subtree(workspace, tmp_path, monkeypatch)
    runtime = RecordingRuntime(repo_root=workspace)

    with pytest.raises(MissingCommandError, match="`git subtree` command is not installed"):
        link_app(workspace, "notes", False, False, runtime)
    with pytest.raises(MissingCommandError, match="`git subtree` command is not installed"):
        install_app(workspace, f"file://{published}", None, runtime, never_bound)

    linked = CliRunner().invoke(main, ["--repo-root", str(workspace), "link", "notes"])
    report = json.loads(linked.output)
    assert (linked.exit_code, report["result"]) == (1, "refused")
    assert "git-subtree" in report["detail"]
    assert _head(workspace) == head_before


def test_the_command_says_what_linking_did_and_what_remains(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    runner = CliRunner()
    root = ["--repo-root", str(workspace)]

    listed_before = runner.invoke(main, [*root, "list"])
    linked = runner.invoke(main, [*root, "link", "notes"])
    again = runner.invoke(main, [*root, "link", "notes", "--refresh"])
    listed_after = runner.invoke(main, [*root, "list"])
    unknown = runner.invoke(main, [*root, "link", "nothing"])

    report = json.loads(linked.output)
    assert (linked.exit_code, report["result"], report["name"], report["package"]) == (0, "linked", "notes", "notes")
    assert report["marker_commit"] == _head(workspace)
    assert (report["is_full_clone_used"], report["is_dev_repository_created"], report["dev_repo_url"]) == (
        False,
        False,
        "",
    )
    assert report["remaining"] == [
        "GitHub sync is not enabled, so no dev repository was created; "
        "pass --create-dev-repo to make one for this app alone"
    ]
    assert report["problems"] == []
    assert (again.exit_code, json.loads(again.output)["result"]) == (0, "already_linked")
    assert [(app["name"], app["state"], app["is_linked"]) for app in json.loads(listed_before.output)["apps"]] == [
        ("notes", "owned", False)
    ]
    assert [(app["name"], app["state"], app["is_linked"]) for app in json.loads(listed_after.output)["apps"]] == [
        ("notes", "owned", True)
    ]
    assert (unknown.exit_code, json.loads(unknown.output)["result"]) == (1, "refused")
