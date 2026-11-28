import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from app_install.cli import main
from app_install.errors import AppChecksFailedError
from app_install.errors import InvalidTemplateVersionError
from app_install.git import run_git
from app_install.mock_runtime_test import RecordingRuntime
from app_install.operations import add_app
from app_install.testing import commit_all
from app_install.testing import make_workspace_repository
from app_install.testing import never_bound
from app_install.testing import write_user_app
from app_install.testing import write_workspace_file
from app_install.verifying import verify_app
from app_manifest.records import load_record


def _workspace_with_notes(tmp_path: Path) -> Path:
    workspace = make_workspace_repository(tmp_path / "workspace")
    add_app(workspace, write_user_app(workspace, "notes"), None, never_bound)
    commit_all(workspace, "Add the notes app")
    return workspace


def test_checks_that_pass_are_recorded_against_the_version_given_and_committed_alone(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    # Someone else's work in progress, which the commit must not take.
    write_workspace_file(workspace, "notes-for-later.md", "unfinished\n")
    runtime = RecordingRuntime(repo_root=workspace, is_committing=True)

    outcome = verify_app(workspace, "notes", "minds-v0.12.0", runtime)

    assert (outcome.verified_template, outcome.problems) == ("minds-v0.12.0", ())
    assert load_record(workspace, "notes").verified_template == "minds-v0.12.0"
    assert runtime.calls == [
        "verify_app notes",
        "commit_paths ['system/config/apps/notes.toml'] 'app-install: notes passes its checks on minds-v0.12.0'",
    ]
    log = run_git(workspace, ["log", "-1", "--format=%an <%ae>%n%s", "--name-only"]).splitlines()
    assert log[:2] == [
        "app-install <app-install@workspace.invalid>",
        "app-install: notes passes its checks on minds-v0.12.0",
    ]
    assert [line for line in log[2:] if line] == ["system/config/apps/notes.toml"]
    assert run_git(workspace, ["status", "--porcelain"]) == "?? notes-for-later.md"


def test_with_no_version_given_the_one_the_checkout_is_on_is_recorded(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    run_git(workspace, ["tag", "minds-v0.11.0"])
    write_workspace_file(workspace, "README.md", "a commit after the release\n")
    commit_all(workspace, "Work after the release")

    outcome = verify_app(workspace, "notes", None, RecordingRuntime(repo_root=workspace, is_committing=True))

    assert outcome.verified_template == "minds-v0.11.0"
    assert load_record(workspace, "notes").verified_template == "minds-v0.11.0"


def test_a_checkout_with_no_release_in_its_history_leaves_the_record_and_says_so(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    before = run_git(workspace, ["rev-parse", "HEAD"])

    outcome = verify_app(workspace, "notes", None, RecordingRuntime(repo_root=workspace, is_committing=True))

    assert outcome.verified_template == ""
    assert "no template release tag" in outcome.problems[0]
    assert load_record(workspace, "notes").verified_template == ""
    assert run_git(workspace, ["rev-parse", "HEAD"]) == before


def test_checks_that_fail_and_a_value_that_is_no_version_leave_the_record_as_it_was(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    failing = RecordingRuntime(repo_root=workspace, verify_failure="1 failed", is_committing=True)

    with pytest.raises(AppChecksFailedError, match="1 failed"):
        verify_app(workspace, "notes", "minds-v0.12.0", failing)
    with pytest.raises(InvalidTemplateVersionError, match="0.12"):
        verify_app(workspace, "notes", "0.12", RecordingRuntime(repo_root=workspace, is_committing=True))

    assert load_record(workspace, "notes").verified_template == ""
    assert run_git(workspace, ["status", "--porcelain"]) == ""


def test_verifying_again_on_the_same_version_writes_nothing(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    verify_app(workspace, "notes", "minds-v0.12.0", RecordingRuntime(repo_root=workspace, is_committing=True))
    before = run_git(workspace, ["rev-parse", "HEAD"])
    runtime = RecordingRuntime(repo_root=workspace, is_committing=True)

    outcome = verify_app(workspace, "notes", "minds-v0.12.0", runtime)

    assert outcome.verified_template == "minds-v0.12.0"
    assert runtime.calls == ["verify_app notes"]
    assert run_git(workspace, ["rev-parse", "HEAD"]) == before


def test_the_command_says_whether_the_checks_passed_and_which_version_it_recorded(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    # An app with no Python project has no environment to sync and no tests to run: its checks pass. One whose
    # project has no lock does not sync from it.
    add_app(workspace, write_user_app(workspace, "notes", is_python=False), None, never_bound)
    add_app(workspace, write_user_app(workspace, "shelf"), None, never_bound)
    commit_all(workspace, "Add the notes and shelf apps")
    root = ["--repo-root", str(workspace)]

    verified = CliRunner().invoke(main, [*root, "verify", "notes", "--template", "minds-v0.12.0"])
    failed = CliRunner().invoke(main, [*root, "verify", "shelf", "--template", "minds-v0.12.0"])

    assert (verified.exit_code, json.loads(verified.output)) == (
        0,
        {"result": "verified", "name": "notes", "verified_template": "minds-v0.12.0", "problems": []},
    )
    assert load_record(workspace, "notes").verified_template == "minds-v0.12.0"
    assert run_git(workspace, ["log", "-1", "--format=%s"]) == "app-install: notes passes its checks on minds-v0.12.0"
    refusal = json.loads(failed.output)
    assert (failed.exit_code, refusal["result"], refusal["name"]) == (1, "tests_failed", "shelf")
    assert "the checks of shelf fail" in refusal["detail"]
    assert load_record(workspace, "shelf").verified_template == ""


def test_the_command_refuses_a_value_that_is_no_template_version(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)

    refused = CliRunner().invoke(main, ["--repo-root", str(workspace), "verify", "notes", "--template", "latest"])

    assert refused.exit_code == 1
    refusal = json.loads(refused.output)
    assert (refusal["result"], refusal["name"]) == ("refused", "notes")
    assert "latest" in refusal["detail"]
