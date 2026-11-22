"""The app pass against local repositories: bare repositories stand in for dev
repositories, and a function for GitHub's answer about each one's visibility."""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from app_manifest.records import (
    AppRecord,
    app_directory_prefix,
    record_path,
    write_record,
)
from click.testing import CliRunner
from imbue.imbue_common.model_update import to_update

from github_sync.app_sync import (
    AppSyncError,
    AppSyncState,
    PushedState,
    VisibilityLedger,
    load_pushed_state,
    pushed_state_path,
    run_app_pass,
)
from github_sync.cli import main
from github_sync.testing import (
    add_owned_app,
    always_private,
    commit_all,
    git_output,
    init_bare_repo,
    init_published_app,
    init_repo,
    init_workspace,
    install_app,
    install_fake_latchkey,
    link_app,
    run_git,
    write_app_file,
)
from github_sync.visibility import (
    VISIBILITY_CHECK_INTERVAL_SECONDS,
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    VISIBILITY_UNKNOWN,
)

pytestmark = pytest.mark.usefixtures("isolated_git_and_gateway_env")


def _linked_app(
    workspace: Path, dev_repository: Path, name: str = "notes"
) -> AppRecord:
    """An app this workspace built, linked, whose record names `dev_repository`."""
    record = add_owned_app(workspace, name, dev_repo_url=str(dev_repository))
    link_app(workspace, record)
    return record


def _states(workspace: Path) -> dict[str, AppSyncState]:
    reports = run_app_pass(workspace, VisibilityLedger(), always_private)
    return {report.name: report.state for report in reports}


def _dev_main(dev_repository: Path) -> str:
    return git_output(dev_repository, "rev-parse", "refs/heads/main")


def test_a_linked_app_is_split_and_pushed_and_then_left_alone(tmp_path: Path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    dev_repository = init_bare_repo(tmp_path / "notes-dev.git")
    record = _linked_app(workspace, dev_repository)
    head_before = git_output(workspace, "rev-parse", "HEAD")

    (report,) = run_app_pass(workspace, VisibilityLedger(), always_private)

    assert (report.name, report.state, report.visibility) == (
        "notes",
        AppSyncState.PUSHED,
        VISIBILITY_PRIVATE,
    )
    # The dev repository holds the app's directory at its root, with the app's own history.
    assert _dev_main(dev_repository) == report.pushed_commit
    assert git_output(dev_repository, "ls-tree", "--name-only", "main") == "app.toml"
    assert (
        git_output(dev_repository, "log", "--format=%s", "main") == "Add the notes app"
    )
    assert load_pushed_state(workspace, "notes") == PushedState(
        dev_repo_url=str(dev_repository),
        pushed_commit=report.pushed_commit,
        pushed_tree=git_output(workspace, "rev-parse", "HEAD:system/apps/notes"),
    )
    # The pass committed nothing and moved nothing in the workspace repository.
    assert git_output(workspace, "rev-parse", "HEAD") == head_before
    assert git_output(workspace, "status", "--porcelain") == ""

    # A commit that leaves the app alone gives the next pass nothing to do.
    (workspace / "README.md").write_text("a workspace, edited\n")
    commit_all(workspace, "Something that is not the app")
    (again,) = run_app_pass(workspace, VisibilityLedger(), always_private)
    assert (again.state, again.pushed_commit) == (
        AppSyncState.UP_TO_DATE,
        report.pushed_commit,
    )
    assert record.dev_repo_url == again.dev_repo_url


def test_more_commits_to_the_app_fast_forward_what_was_pushed(tmp_path: Path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    dev_repository = init_bare_repo(tmp_path / "notes-dev.git")
    record = _linked_app(workspace, dev_repository)
    (first,) = run_app_pass(workspace, VisibilityLedger(), always_private)

    write_app_file(workspace, record, "notes.py", "GREETING = 'hello'\n")
    (workspace / "README.md").write_text("a workspace, edited\n")
    commit_all(workspace, "Greet, and edit the workspace too")
    (second,) = run_app_pass(workspace, VisibilityLedger(), always_private)

    assert second.state == AppSyncState.PUSHED
    assert _dev_main(dev_repository) == second.pushed_commit != first.pushed_commit
    run_git(dev_repository, "merge-base", "--is-ancestor", first.pushed_commit, "main")
    # The dev repository carries the message of the workspace commit, and none of the workspace's files.
    assert (
        git_output(dev_repository, "log", "--format=%s", "-1", "main")
        == "Greet, and edit the workspace too"
    )
    assert git_output(
        dev_repository, "ls-tree", "-r", "--name-only", "main"
    ).splitlines() == [
        "app.toml",
        "notes.py",
    ]


def test_a_lost_state_file_costs_a_split_and_a_push_that_changes_nothing(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    dev_repository = init_bare_repo(tmp_path / "notes-dev.git")
    _linked_app(workspace, dev_repository)
    (first,) = run_app_pass(workspace, VisibilityLedger(), always_private)
    state_before = load_pushed_state(workspace, "notes")
    pushed_state_path(workspace, "notes").unlink()

    (recovered,) = run_app_pass(workspace, VisibilityLedger(), always_private)

    assert (recovered.state, recovered.pushed_commit) == (
        AppSyncState.PUSHED,
        first.pushed_commit,
    )
    assert _dev_main(dev_repository) == first.pushed_commit
    assert load_pushed_state(workspace, "notes") == state_before


def test_an_unreadable_state_file_is_treated_as_lost(tmp_path: Path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    dev_repository = init_bare_repo(tmp_path / "notes-dev.git")
    _linked_app(workspace, dev_repository)
    pushed_state_path(workspace, "notes").parent.mkdir(parents=True)
    pushed_state_path(workspace, "notes").write_text("{not json")

    assert _states(workspace) == {"notes": AppSyncState.PUSHED}
    assert load_pushed_state(workspace, "notes") is not None


def test_a_push_that_is_not_a_fast_forward_is_refused_and_nothing_is_overwritten(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    dev_repository = init_bare_repo(tmp_path / "notes-dev.git")
    _linked_app(workspace, dev_repository)
    # Another workspace that owns the same app got there first, with history of its own.
    other = tmp_path / "another-workspace-split"
    init_repo(other)
    (other / "app.toml").write_text('name = "notes"\nversion = "9.9.9"\n')
    theirs = commit_all(other, "The other workspace's notes")
    run_git(other, "push", "-q", str(dev_repository), "HEAD:refs/heads/main")

    (report,) = run_app_pass(workspace, VisibilityLedger(), always_private)

    assert report.state == AppSyncState.PUSH_REFUSED
    assert "another workspace" in report.detail
    assert _dev_main(dev_repository) == theirs
    assert load_pushed_state(workspace, "notes") is None
    # Refused again on the next pass, rather than forced through.
    assert _states(workspace) == {"notes": AppSyncState.PUSH_REFUSED}
    assert _dev_main(dev_repository) == theirs


def test_a_dev_repository_found_public_stops_its_own_pushes_and_no_other_apps(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    public_repository = init_bare_repo(tmp_path / "notes-dev.git")
    private_repository = init_bare_repo(tmp_path / "shelf-dev.git")
    _linked_app(workspace, public_repository, "notes")
    _linked_app(workspace, private_repository, "shelf")

    def visibility_of(repo_url: str) -> str:
        return (
            VISIBILITY_PUBLIC
            if repo_url == str(public_repository)
            else VISIBILITY_PRIVATE
        )

    notes, shelf = run_app_pass(workspace, VisibilityLedger(), visibility_of)

    assert (notes.state, notes.visibility) == (
        AppSyncState.NOT_PRIVATE,
        VISIBILITY_PUBLIC,
    )
    assert "public" in notes.detail
    assert git_output(public_repository, "for-each-ref") == ""
    assert shelf.state == AppSyncState.PUSHED
    assert _dev_main(private_repository) == shelf.pushed_commit


def test_a_dev_repository_not_yet_confirmed_private_is_held(tmp_path: Path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    dev_repository = init_bare_repo(tmp_path / "notes-dev.git")
    _linked_app(workspace, dev_repository)

    def never_answers(repo_url: str) -> str:
        return VISIBILITY_UNKNOWN

    (report,) = run_app_pass(workspace, VisibilityLedger(), never_answers)

    assert report.state == AppSyncState.VISIBILITY_UNKNOWN
    assert git_output(dev_repository, "for-each-ref") == ""


def test_a_confirmed_visibility_is_kept_until_it_is_stale_and_through_a_failed_check() -> (
    None
):
    asked: list[str] = []
    answers = [VISIBILITY_PRIVATE, VISIBILITY_UNKNOWN, VISIBILITY_PUBLIC]

    def next_answer(repo_url: str) -> str:
        asked.append(repo_url)
        return answers[len(asked) - 1]

    ledger = VisibilityLedger()
    confirmed_at = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    stale_at = confirmed_at + timedelta(seconds=VISIBILITY_CHECK_INTERVAL_SECONDS + 1)
    url = "https://github.com/someone/notes-dev"

    assert ledger.refresh(url, next_answer, confirmed_at) == VISIBILITY_PRIVATE
    # A fresh answer is not asked for again.
    assert (
        ledger.refresh(url, next_answer, confirmed_at + timedelta(seconds=60))
        == VISIBILITY_PRIVATE
    )
    assert len(asked) == 1
    # A check that fails outright keeps the confirmed answer, and is retried.
    assert ledger.refresh(url, next_answer, stale_at) == VISIBILITY_PRIVATE
    assert ledger.refresh(url, next_answer, stale_at) == VISIBILITY_PUBLIC
    assert len(asked) == 3


def test_apps_with_no_dev_repository_or_no_marker_are_skipped_and_named(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    dev_repository = init_bare_repo(tmp_path / "drafts-dev.git")
    add_owned_app(workspace, "shelf")
    add_owned_app(workspace, "drafts", dev_repo_url=str(dev_repository))
    untouched = install_app(
        workspace, init_published_app(tmp_path / "published-notes", "notes"), "notes"
    )
    changed = install_app(
        workspace, init_published_app(tmp_path / "published-tasks", "tasks"), "tasks"
    )
    write_app_file(workspace, untouched, "uv.lock", "relocked in this workspace\n")
    write_app_file(workspace, changed, "notes.py", "GREETING = 'good morning'\n")
    commit_all(workspace, "Relock one installed app and change the other")

    reports = {
        report.name: report
        for report in run_app_pass(workspace, VisibilityLedger(), always_private)
    }

    assert {name: report.state for name, report in reports.items()} == {
        "drafts": AppSyncState.NOT_LINKED,
        "notes": AppSyncState.UNMODIFIED,
        "shelf": AppSyncState.NO_DEV_REPOSITORY,
        "tasks": AppSyncState.NO_DEV_REPOSITORY,
    }
    assert "app-install link drafts" in reports["drafts"].detail
    assert "app-install link shelf" in reports["shelf"].detail
    assert git_output(dev_repository, "for-each-ref") == ""


def test_a_modified_installed_apps_history_descends_from_what_it_was_installed_from(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    published = init_published_app(tmp_path / "published-notes", "notes")
    dev_repository = init_bare_repo(tmp_path / "notes-dev.git")
    installed = install_app(workspace, published, "notes")
    record = installed.model_copy_update(
        to_update(installed.field_ref().dev_repo_url, str(dev_repository))
    )
    write_record(workspace, record)
    write_app_file(workspace, record, "notes.py", "GREETING = 'good morning'\n")
    commit_all(workspace, "Change the greeting")

    (report,) = run_app_pass(workspace, VisibilityLedger(), always_private)

    assert report.state == AppSyncState.PUSHED
    # The adopter's repository shows where the app came from: the publisher's
    # commits, then the adopter's own.
    run_git(
        dev_repository, "merge-base", "--is-ancestor", record.upstream_commit, "main"
    )
    assert git_output(dev_repository, "log", "--format=%s", "main").splitlines() == [
        "Change the greeting",
        "notes: second version",
        "notes: first version",
    ]
    assert (
        git_output(dev_repository, "show", "main:notes.py")
        == "GREETING = 'good morning'"
    )


def test_a_failure_in_one_app_does_not_stop_the_next(tmp_path: Path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    dev_repository = init_bare_repo(tmp_path / "shelf-dev.git")
    _linked_app(workspace, tmp_path / "no-such-repository.git", "notes")
    _linked_app(workspace, dev_repository, "shelf")
    record_path(workspace, "broken").write_text("this is not a record\n")

    reports = {
        report.name: report
        for report in run_app_pass(workspace, VisibilityLedger(), always_private)
    }

    assert {name: report.state for name, report in reports.items()} == {
        "broken": AppSyncState.FAILED,
        "notes": AppSyncState.FAILED,
        "shelf": AppSyncState.PUSHED,
    }
    assert "no-such-repository.git" in reports["notes"].detail
    assert "broken.toml" in reports["broken"].detail
    assert load_pushed_state(workspace, "notes") is None


def test_the_workspaces_own_history_is_never_pushed_to_a_dev_repository(
    tmp_path: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    initial = git_output(workspace, "rev-parse", "HEAD")
    dev_repository = init_bare_repo(tmp_path / "notes-dev.git")
    record = _linked_app(workspace, dev_repository)
    assert _states(workspace) == {"notes": AppSyncState.PUSHED}
    pushed_before = _dev_main(dev_repository)
    # The app's directory is removed and made again, as an uninstall followed
    # by a new app of the same name leaves it.
    shutil.rmtree(workspace / app_directory_prefix(record))
    commit_all(workspace, "Remove the notes app")
    write_app_file(workspace, record, "app.toml", 'name = "notes"\nversion = "2"\n')
    commit_all(workspace, "Build the notes app again")

    (report,) = run_app_pass(workspace, VisibilityLedger(), always_private)

    assert report.state == AppSyncState.FAILED
    assert "removed and made again" in report.detail
    assert _dev_main(dev_repository) == pushed_before
    assert (
        git_output(dev_repository, "rev-list", "--all", "--count", "--", "README.md")
        == "0"
    )
    assert initial not in git_output(dev_repository, "rev-list", "--all")


def test_the_pass_runs_against_the_primary_checkout_only(tmp_path: Path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    _linked_app(workspace, init_bare_repo(tmp_path / "notes-dev.git"))
    worktree = tmp_path / "worker"
    run_git(workspace, "worktree", "add", "-q", "--detach", str(worktree))

    with pytest.raises(AppSyncError, match="primary checkout"):
        run_app_pass(worktree, VisibilityLedger(), always_private)
    with pytest.raises(AppSyncError, match="not a git checkout"):
        run_app_pass(tmp_path, VisibilityLedger(), always_private)


def test_sync_app_brings_one_app_up_to_date_without_workspace_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
    fake_latchkey_bin: Path,
) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    dev_repository = init_bare_repo(tmp_path / "notes-dev.git")
    dev_repo_url = "https://github.com/someone/notes-dev"
    record = add_owned_app(workspace, "notes", dev_repo_url=dev_repo_url)
    link_app(workspace, record)
    add_owned_app(workspace, "shelf")
    # No test reaches GitHub: the dev repository's URL resolves to the local one.
    run_git(
        workspace, "config", "--global", f"url.{dev_repository}.insteadOf", dev_repo_url
    )
    install_fake_latchkey(fake_latchkey_bin, "echo '{\"private\": true}'")
    monkeypatch.chdir(workspace)
    runner = CliRunner()

    pushed = runner.invoke(main, ["sync-app", "notes"])
    again = runner.invoke(main, ["sync-app", "notes"])
    no_repository = runner.invoke(main, ["sync-app", "shelf"])
    unknown = runner.invoke(main, ["sync-app", "nothing"])

    report = json.loads(pushed.output)
    assert (pushed.exit_code, report["result"], report["dev_repo_url"]) == (
        0,
        "pushed",
        dev_repo_url,
    )
    assert _dev_main(dev_repository) == report["pushed_commit"]
    assert (again.exit_code, json.loads(again.output)["result"]) == (0, "up_to_date")
    assert (no_repository.exit_code, json.loads(no_repository.output)["result"]) == (
        1,
        "no_dev_repository",
    )
    assert (unknown.exit_code, json.loads(unknown.output)["result"]) == (1, "refused")
    # GitHub sync was never enabled for this workspace.
    assert not (workspace / "data" / "system" / "github_sync.toml").exists()
