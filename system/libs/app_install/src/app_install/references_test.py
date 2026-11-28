import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from app_install.cli import main
from app_install.errors import RecordReferenceError
from app_install.git import run_git
from app_install.mock_runtime_test import RecordingRuntime
from app_install.operations import add_app
from app_install.references import add_reference
from app_install.references import remove_reference
from app_install.testing import commit_all
from app_install.testing import make_workspace_repository
from app_install.testing import never_bound
from app_install.testing import write_app_file
from app_install.testing import write_user_app
from app_install.testing import write_workspace_file
from app_manifest.errors import AppRecordNotFoundError
from app_manifest.manifest import ManifestReadMode
from app_manifest.manifest import load_manifest
from app_manifest.records import RecordReference
from app_manifest.records import load_record
from app_manifest.records import record_path
from app_manifest.scope import compute_app_scope


def _workspace_with_notes(tmp_path: Path) -> Path:
    workspace = make_workspace_repository(tmp_path / "workspace")
    add_app(workspace, write_user_app(workspace, "notes"), None, never_bound)
    write_workspace_file(workspace, ".agents/skills/weekly-review/SKILL.md", "# Weekly review\n")
    commit_all(workspace, "Add the notes app and a skill that reads it")
    return workspace


def test_a_reference_is_written_to_the_record_and_committed_alone_under_the_installers_author(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    # Someone else's work in progress, which the commit must not take.
    write_workspace_file(workspace, "notes-for-later.md", "unfinished\n")
    runtime = RecordingRuntime(repo_root=workspace, is_committing=True)

    outcome = add_reference(workspace, "notes", ".agents/skills/weekly-review", "Calls GET /api/notes", runtime)

    reference = RecordReference(path=".agents/skills/weekly-review", note="Calls GET /api/notes")
    assert outcome.reference == reference
    assert outcome.problems == ()
    assert load_record(workspace, "notes").references == (reference,)
    log = run_git(workspace, ["log", "-1", "--format=%an <%ae>%n%s", "--name-only"]).splitlines()
    assert log[:2] == [
        "app-install <app-install@workspace.invalid>",
        "app-install: record that .agents/skills/weekly-review uses notes",
    ]
    assert [line for line in log[2:] if line] == ["system/config/apps/notes.toml"]
    assert run_git(workspace, ["status", "--porcelain"]) == "?? notes-for-later.md"
    # The footprint code reads the reference from the record, as part of the app.
    manifest_file = workspace / "system" / "apps" / "notes" / "app.toml"
    scope = compute_app_scope(workspace, manifest_file, load_manifest(manifest_file, mode=ManifestReadMode.STRICT))
    assert [str(listed.path) for listed in scope.references] == [".agents/skills/weekly-review"]


def test_a_second_reference_keeps_the_first_and_removing_one_leaves_the_other(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    write_workspace_file(workspace, "system/scripts/export_notes.py", "print('notes')\n")
    runtime = RecordingRuntime(repo_root=workspace, is_committing=True)
    add_reference(workspace, "notes", ".agents/skills/weekly-review", "", runtime)
    add_reference(workspace, "notes", "system/scripts/export_notes.py", "Reads the notes file", runtime)

    outcome = remove_reference(workspace, "notes", ".agents/skills/weekly-review", runtime)

    assert outcome.reference == RecordReference(path=".agents/skills/weekly-review", note="")
    assert load_record(workspace, "notes").references == (
        RecordReference(path="system/scripts/export_notes.py", note="Reads the notes file"),
    )
    assert run_git(workspace, ["log", "-1", "--format=%s"]) == (
        "app-install: record that .agents/skills/weekly-review no longer uses notes"
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("system/scripts/missing.py", "does not exist"),
        ("system/apps/notes/src", "inside the app's own directory"),
        ("system/apps/digest/app.toml", "names another app's directory"),
        (".claude/skills/weekly-review", "goes through the symlink"),
        ("system/vendor/tk", "nothing under 'system/vendor/' can be referenced"),
        ("data/notes.json", "nothing under 'data/' can be referenced"),
        (".agents/skills/*", "a reference is a literal path"),
        ("/etc/passwd", "reference path"),
    ],
)
def test_a_reference_is_held_to_the_rules_the_footprint_code_reads_it_by(
    tmp_path: Path, path: str, expected: str
) -> None:
    workspace = _workspace_with_notes(tmp_path)
    add_app(workspace, write_user_app(workspace, "digest"), None, never_bound)
    write_app_file(workspace, "notes", "src/notes/runner.py")
    write_workspace_file(workspace, "system/vendor/tk/README.md")
    write_workspace_file(workspace, "data/notes.json", "[]")
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "skills").symlink_to("../.agents/skills")
    before = record_path(workspace, "notes").read_text()
    runtime = RecordingRuntime(repo_root=workspace, is_committing=True)

    with pytest.raises(RecordReferenceError, match=expected):
        add_reference(workspace, "notes", path, "", runtime)

    assert record_path(workspace, "notes").read_text() == before
    assert runtime.calls == []


def test_a_duplicate_and_a_reference_that_is_not_listed_are_refused(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    runtime = RecordingRuntime(repo_root=workspace)
    add_reference(workspace, "notes", ".agents/skills/weekly-review", "first", runtime)

    with pytest.raises(RecordReferenceError, match="already lists"):
        add_reference(workspace, "notes", ".agents/skills/weekly-review", "second", runtime)
    with pytest.raises(RecordReferenceError, match="does not list"):
        remove_reference(workspace, "notes", "system/scripts/export_notes.py", runtime)
    with pytest.raises(AppRecordNotFoundError):
        add_reference(workspace, "digest", ".agents/skills/weekly-review", "", runtime)

    assert load_record(workspace, "notes").references == (
        RecordReference(path=".agents/skills/weekly-review", note="first"),
    )


def test_a_commit_that_fails_is_a_problem_and_the_record_is_written_all_the_same(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    runtime = RecordingRuntime(repo_root=workspace, commit_failure="fatal: unable to write new index file")

    outcome = add_reference(workspace, "notes", ".agents/skills/weekly-review", "", runtime)

    assert outcome.problems == (
        "the app's record is written but was not committed: fatal: unable to write new index file",
    )
    assert len(load_record(workspace, "notes").references) == 1


def test_the_command_adds_and_removes_a_reference_and_says_why_it_refuses_one(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    arguments = ["--repo-root", str(workspace), "reference"]

    added = CliRunner().invoke(
        main, [*arguments, "add", "notes", ".agents/skills/weekly-review", "--note", "Calls GET /api/notes"]
    )
    assert added.exit_code == 0
    assert json.loads(added.output) == {
        "result": "reference_added",
        "name": "notes",
        "path": ".agents/skills/weekly-review",
        "note": "Calls GET /api/notes",
        "references": [{"path": ".agents/skills/weekly-review", "note": "Calls GET /api/notes"}],
        "problems": [],
    }

    refused = CliRunner().invoke(main, [*arguments, "add", "notes", "system/scripts/missing.py"])
    assert refused.exit_code == 1
    refusal = json.loads(refused.output)
    assert refusal["result"] == "refused"
    assert refusal["path"] == "system/scripts/missing.py"
    assert "does not exist" in refusal["detail"]

    removed = CliRunner().invoke(main, [*arguments, "remove", "notes", ".agents/skills/weekly-review"])
    assert removed.exit_code == 0
    assert json.loads(removed.output) == {
        "result": "reference_removed",
        "name": "notes",
        "path": ".agents/skills/weekly-review",
        "note": "Calls GET /api/notes",
        "references": [],
        "problems": [],
    }
    assert run_git(workspace, ["status", "--porcelain"]) == ""
