import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from app_install.cli import main
from app_install.errors import CollisionError
from app_install.errors import NotAnAppError
from app_install.errors import RepositoryUnreachableError
from app_install.git import run_git
from app_install.installing import install_app
from app_install.mock_runtime_test import RecordingRuntime
from app_install.operations import check
from app_install.testing import commit_all
from app_install.testing import make_published_app_repository
from app_install.testing import make_workspace_repository
from app_install.testing import never_bound
from app_install.testing import write_workspace_file
from app_manifest.records import AppOrigin
from app_manifest.records import load_all_records
from app_manifest.records import load_record

_NOTES_NEEDS = """
version = "0.2.0"

[[secrets]]
file = "notes"
variables = ["NOTES_API_KEY"]
note = "An API key from the account's settings page"

[[permissions]]
scope = "slack-api"
permission = "slack-read-all"

[[dependencies]]
name = "datalib"

[[adaptation]]
summary = "The digest channel is hardcoded"
"""


@pytest.fixture(autouse=True)
def _no_registry_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDS_APPS_FILE", raising=False)


def _published_notes(tmp_path: Path) -> tuple[Path, str]:
    """A published notes app with two released versions and unreleased work on main; its URL and the commit of
    the newer release."""
    published = make_published_app_repository(tmp_path / "published-notes", "notes", '\nversion = "0.1.0"\n')
    run_git(published, ["tag", "v0.1.0"])
    (published / "app.toml").write_text('name = "notes"\ndisplay_name = "Notes"\nicon = "icon.svg"\n' + _NOTES_NEEDS)
    (published / "skills" / "weekly-review").mkdir(parents=True)
    (published / "skills" / "weekly-review" / "SKILL.md").write_text("# Weekly review\n")
    released = commit_all(published, "notes 0.2.0")
    run_git(published, ["tag", "v0.2.0"])
    (published / "UNRELEASED.md").write_text("not in any version\n")
    commit_all(published, "work after the release")
    return published, released


def _state(workspace: Path) -> tuple[str, str, str]:
    """The workspace repository's commit, what is uncommitted in it, and its worktrees."""
    return (
        run_git(workspace, ["rev-parse", "HEAD"]),
        run_git(workspace, ["status", "--porcelain"]),
        run_git(workspace, ["worktree", "list", "--porcelain"]),
    )


def test_installing_takes_the_latest_version_and_lands_the_app_registered_and_generated(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    published, released = _published_notes(tmp_path)
    runtime = RecordingRuntime(repo_root=workspace)

    outcome = install_app(workspace, f"file://{published}", None, runtime, never_bound)

    record = load_record(workspace, "notes")
    assert outcome.record == record
    assert (record.origin, record.package, record.port) == (AppOrigin.INSTALLED, "notes", 8080)
    assert (record.upstream_url, record.upstream_commit, record.upstream_version) == (
        f"file://{published}",
        released,
        "0.2.0",
    )
    assert record.upstream_tree == run_git(published, ["rev-parse", f"{released}^{{tree}}"])
    assert outcome.ref == "v0.2.0"
    # The released tree is in the workspace, and nothing past the release.
    assert (workspace / "system" / "apps" / "notes" / "skills" / "weekly-review" / "SKILL.md").is_file()
    assert not (workspace / "system" / "apps" / "notes" / "UNRELEASED.md").exists()
    assert (workspace / ".agents" / "skills" / "weekly-review" / "SKILL.md").read_text() == "# Weekly review\n"
    assert "--url http://localhost:8080" in (workspace / "system" / "supervisord.conf.d" / "notes.conf").read_text()
    # Everything is committed, by the installer, with the publisher's history left out.
    assert run_git(workspace, ["status", "--porcelain"]) == ""
    assert run_git(workspace, ["log", "--format=%an", "-1"]) == "app-install"
    assert "notes: first version" not in run_git(workspace, ["log", "--format=%s"])
    assert run_git(workspace, ["rev-parse", "refs/app-upstream/notes"]) == released
    assert len(run_git(workspace, ["worktree", "list", "--porcelain"]).split("\n\n")) == 1
    assert check(workspace) == ()
    # What the app still needs is reported, and a program waiting on its secret file is not waited for.
    assert outcome.missing_secret_files == ("data/.secrets/notes.env",)
    assert outcome.missing_dependencies == ("datalib",)
    assert outcome.is_answering is False
    assert runtime.calls == ["install_environment apt=[]", "reload_programs"]


def test_an_app_with_everything_it_needs_is_waited_for_until_it_answers(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    published = make_published_app_repository(tmp_path / "published-shelf", "shelf")
    runtime = RecordingRuntime(repo_root=workspace)

    outcome = install_app(workspace, f"file://{published}", None, runtime, never_bound)

    # With no version tag the app's main branch is what is installed, and its version is not known.
    assert (outcome.ref, outcome.record.upstream_version, outcome.is_answering) == ("main", "", True)
    assert runtime.calls == ["install_environment apt=[]", "reload_programs", "wait_for_port 8080"]


def test_a_named_ref_is_the_one_installed(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    published, _ = _published_notes(tmp_path)

    outcome = install_app(
        workspace, f"file://{published}", "v0.1.0", RecordingRuntime(repo_root=workspace), never_bound
    )

    assert (outcome.ref, outcome.record.upstream_version) == ("v0.1.0", "0.1.0")
    assert not (workspace / "system" / "apps" / "notes" / "skills").exists()


def test_the_users_uncommitted_work_is_left_alone(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    published = make_published_app_repository(tmp_path / "published-shelf", "shelf")
    write_workspace_file(workspace, "README.md", "a workspace, being edited\n")

    install_app(workspace, f"file://{published}", None, RecordingRuntime(repo_root=workspace), never_bound)

    assert run_git(workspace, ["status", "--porcelain"]) == "M README.md"
    assert (workspace / "README.md").read_text() == "a workspace, being edited\n"
    assert (workspace / "system" / "apps" / "shelf" / "app.toml").is_file()


def test_a_repository_that_holds_no_app_is_refused_and_the_workspace_is_unchanged(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    not_an_app = make_workspace_repository(tmp_path / "just-a-repository")
    before = _state(workspace)

    with pytest.raises(NotAnAppError, match="no app.toml"):
        install_app(workspace, f"file://{not_an_app}", None, RecordingRuntime(repo_root=workspace), never_bound)

    assert _state(workspace) == before


def test_an_app_that_would_collide_is_refused_and_the_workspace_is_unchanged(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    write_workspace_file(workspace, ".agents/skills/weekly-review/SKILL.md", "# The workspace's own\n")
    commit_all(workspace, "a skill of the workspace's own")
    published, _ = _published_notes(tmp_path)
    before = _state(workspace)

    with pytest.raises(CollisionError) as raised:
        install_app(workspace, f"file://{published}", None, RecordingRuntime(repo_root=workspace), never_bound)

    assert raised.value.problems == ("the skill 'weekly-review' already exists at .agents/skills/weekly-review",)
    assert _state(workspace) == before
    assert load_all_records(workspace) == ()
    assert not (workspace / "system" / "apps" / "notes").exists()


def test_an_app_already_in_the_workspace_is_not_installed_twice(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    published = make_published_app_repository(tmp_path / "published-shelf", "shelf")
    install_app(workspace, f"file://{published}", None, RecordingRuntime(repo_root=workspace), never_bound)
    before = _state(workspace)

    with pytest.raises(CollisionError, match="already in this workspace"):
        install_app(workspace, f"file://{published}", None, RecordingRuntime(repo_root=workspace), never_bound)

    assert _state(workspace) == before


def test_a_repository_that_cannot_be_read_is_reported_as_unreachable(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")

    with pytest.raises(RepositoryUnreachableError) as raised:
        install_app(
            workspace, f"file://{tmp_path / 'nowhere'}", None, RecordingRuntime(repo_root=workspace), never_bound
        )

    assert raised.value.is_access_needed is False


def test_the_command_reports_what_was_installed_and_what_the_app_still_needs(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    published, released = _published_notes(tmp_path)
    runner = CliRunner()
    root = ["--repo-root", str(workspace)]

    installed = runner.invoke(main, [*root, "install", f"file://{published}"])
    again = runner.invoke(main, [*root, "install", f"file://{published}"])
    not_an_app = runner.invoke(main, [*root, "install", f"file://{workspace}"])

    report = json.loads(installed.output)
    assert (installed.exit_code, report["result"], report["name"], report["version"]) == (
        0,
        "installed",
        "notes",
        "0.2.0",
    )
    assert (report["ref"], report["upstream_commit"], report["programs"]) == ("v0.2.0", released, ["notes"])
    assert report["needs"]["secrets"] == [
        {
            "file": "notes",
            "variables": ["NOTES_API_KEY"],
            "note": "An API key from the account's settings page",
            "is_present": False,
        }
    ]
    assert report["needs"]["permissions"] == [{"scope": "slack-api", "permission": "slack-read-all", "note": ""}]
    assert report["needs"]["missing_dependencies"] == ["datalib"]
    assert [note["summary"] for note in report["needs"]["adaptation"]] == ["The digest channel is hardcoded"]
    # No supervisord runs beside a bare repository, which the report says without failing the install.
    assert report["is_answering"] is False
    assert report["problems"][0].startswith("supervisord did not start the app's programs")
    assert (again.exit_code, json.loads(again.output)["result"]) == (1, "collision")
    assert (not_an_app.exit_code, json.loads(not_an_app.output)["result"]) == (1, "not_an_app")
