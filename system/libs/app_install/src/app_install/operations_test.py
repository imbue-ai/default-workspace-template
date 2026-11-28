import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from imbue.imbue_common.model_update import to_update

from app_install.cli import main
from app_install.errors import AppAlreadyRegisteredError
from app_install.errors import AppNotFoundError
from app_install.errors import CollisionError
from app_install.errors import PortUnavailableError
from app_install.git import run_git
from app_install.mock_runtime_test import RecordingRuntime
from app_install.operations import add_app
from app_install.operations import check
from app_install.operations import list_apps
from app_install.operations import regenerate
from app_install.operations import uninstall_app
from app_install.runtime import WorkspaceRuntime
from app_install.testing import commit_all
from app_install.testing import make_workspace_repository
from app_install.testing import never_bound
from app_install.testing import write_app_file
from app_install.testing import write_user_app
from app_install.testing import write_workspace_file
from app_manifest.errors import AppRecordNotFoundError
from app_manifest.errors import ManifestLoadError
from app_manifest.records import AppOrigin
from app_manifest.records import AppRecord
from app_manifest.records import load_record
from app_manifest.records import record_path
from app_manifest.records import write_record


def _git(repo_root: Path, *arguments: str, committer_date: str | None = None) -> None:
    environment = dict(os.environ)
    if committer_date is not None:
        environment["GIT_COMMITTER_DATE"] = committer_date
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *arguments],
        cwd=repo_root,
        env=environment,
        check=True,
        capture_output=True,
    )


def test_adding_an_app_writes_its_record_its_program_and_its_skill_links(tmp_path: Path) -> None:
    package = write_user_app(tmp_path, "inbox-status")
    write_app_file(tmp_path, package, "skills/triage-inbox/SKILL.md", "# Triage\n")

    outcome = add_app(tmp_path, package, None, never_bound)

    assert outcome.record == AppRecord(name="inbox-status", package="inbox_status", port=8080, origin=AppOrigin.LOCAL)
    assert load_record(tmp_path, "inbox-status") == outcome.record
    assert sorted(outcome.changed_paths) == [
        ".agents/skills/triage-inbox",
        "system/config/apps/inbox-status.toml",
        "system/supervisord.conf.d/inbox-status.conf",
    ]
    conf = (tmp_path / "system" / "supervisord.conf.d" / "inbox-status.conf").read_text()
    assert "--url http://localhost:8080" in conf
    assert 'environment=INBOX_STATUS_PORT="8080"' in conf
    assert (tmp_path / ".agents" / "skills" / "triage-inbox" / "SKILL.md").read_text() == "# Triage\n"
    assert check(tmp_path) == ()


def test_a_second_app_gets_the_next_port_and_a_requested_port_is_honoured(tmp_path: Path) -> None:
    add_app(tmp_path, write_user_app(tmp_path, "notes"), None, never_bound)

    second = add_app(tmp_path, write_user_app(tmp_path, "digest"), None, never_bound)
    third = add_app(tmp_path, write_user_app(tmp_path, "shelf"), 8093, never_bound)

    assert (second.record.port, third.record.port) == (8081, 8093)


def test_an_app_cannot_be_added_twice_and_a_directory_with_no_manifest_is_not_an_app(tmp_path: Path) -> None:
    package = write_user_app(tmp_path, "notes")
    add_app(tmp_path, package, None, never_bound)

    with pytest.raises(AppAlreadyRegisteredError, match="system/apps/notes"):
        add_app(tmp_path, package, None, never_bound)
    with pytest.raises(AppNotFoundError, match="system/apps/missing"):
        add_app(tmp_path, "missing", None, never_bound)


def test_a_taken_port_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    add_app(tmp_path, write_user_app(tmp_path, "notes"), None, never_bound)
    package = write_user_app(tmp_path, "digest")

    with pytest.raises(PortUnavailableError):
        add_app(tmp_path, package, 8080, never_bound)

    assert not (tmp_path / "system" / "config" / "apps" / "digest.toml").exists()
    assert not (tmp_path / "system" / "supervisord.conf.d" / "digest.conf").exists()


@pytest.mark.parametrize(
    "extra_manifest",
    [
        pytest.param('\n[[references]]\npath = "docs/notes-guide.md"\nnote = "Describes the app"\n', id="references"),
        pytest.param('\n[wiring]\nprograms = ["notes-display"]\n', id="wiring"),
    ],
)
def test_a_manifest_with_a_table_a_recorded_app_does_not_declare_is_refused_and_nothing_is_written(
    tmp_path: Path, extra_manifest: str
) -> None:
    write_workspace_file(tmp_path, "docs/notes-guide.md", "# Notes\n")
    write_workspace_file(
        tmp_path, "system/supervisord.conf.d/notes-display.conf", "[program:notes-display]\ncommand=display\n"
    )
    package = write_user_app(tmp_path, "notes", extra_manifest)

    with pytest.raises(ManifestLoadError, match="an app with a record declares no"):
        add_app(tmp_path, package, None, never_bound)

    assert not (tmp_path / "system" / "config" / "apps" / "notes.toml").exists()
    assert not (tmp_path / "system" / "supervisord.conf.d" / "notes.conf").exists()


def test_every_collision_is_named_before_anything_is_written(tmp_path: Path) -> None:
    add_app(tmp_path, write_user_app(tmp_path, "notes"), None, never_bound)
    write_app_file(tmp_path, "notes", "routines/nightly-sync.toml")
    write_app_file(tmp_path, "notes", "env.d/2100-notes-poppler.sh")
    write_workspace_file(tmp_path, ".agents/skills/weekly-review/SKILL.md", "# The workspace's own\n")
    write_workspace_file(tmp_path, "system/routines/morning-digest.toml")
    write_workspace_file(tmp_path, "system/scripts/env.d/2000-screen.sh")
    write_workspace_file(
        tmp_path, "system/supervisord.conf.d/digest-mailer.conf", "[program:digest-mailer]\ncommand=mailer\n"
    )
    # A second directory that claims the name `notes`, a program, a script, a skill, two routines and two units
    # that something else already holds.
    extra = '\n[[services]]\nrole = "mailer"\ncommand = "digest-mailer"\n'
    package = "digest"
    write_user_app(tmp_path, "digest", extra)
    (tmp_path / "system" / "apps" / package / "app.toml").write_text(
        'name = "notes"\ndisplay_name = "Notes too"\nicon = "icon.svg"\nprogram = "digest"\n'
        '\n[[services]]\nrole = "indexer"\ncommand = "x"\n'
    )
    (tmp_path / "system" / "apps" / package / "pyproject.toml").write_text(
        '[project]\nname = "digest"\nversion = "0.1.0"\n\n[project.scripts]\nnotes = "digest.runner:main"\n'
    )
    write_workspace_file(
        tmp_path, "system/supervisord.conf.d/notes-indexer.conf", "[eventlistener:notes-indexer]\ncommand=x\n"
    )
    for relative_path in (
        "skills/weekly-review/SKILL.md",
        "routines/nightly-sync.toml",
        "routines/morning-digest.toml",
        "env.d/2100-notes-poppler.sh",
        "env.d/2000-screen.sh",
    ):
        write_app_file(tmp_path, package, relative_path)

    with pytest.raises(CollisionError) as raised:
        add_app(tmp_path, package, None, never_bound)

    assert raised.value.problems == (
        "the name 'notes' is already the app at system/apps/notes",
        "the program 'notes-indexer' is already declared in system/supervisord.conf.d/notes-indexer.conf",
        "the console script 'notes' is already declared by system/apps/notes",
        "the skill 'weekly-review' already exists at .agents/skills/weekly-review",
        "the routine 'morning-digest' already exists at system/routines/morning-digest.toml",
        "the routine 'nightly-sync' already exists at system/apps/notes/routines/nightly-sync.toml",
        "the environment unit '2000-screen.sh' already exists at system/scripts/env.d/2000-screen.sh",
        "the environment unit '2100-notes-poppler.sh' already exists at system/apps/notes/env.d/2100-notes-poppler.sh",
    )
    assert not (tmp_path / "system" / "supervisord.conf.d" / "digest.conf").exists()


def test_check_reports_a_hand_edited_program_and_regenerate_puts_it_back(tmp_path: Path) -> None:
    add_app(tmp_path, write_user_app(tmp_path, "notes"), None, never_bound)
    conf = tmp_path / "system" / "supervisord.conf.d" / "notes.conf"
    generated = conf.read_text()
    conf.write_text(generated.replace("autorestart=true", "autorestart=false"))

    problems = check(tmp_path)
    outcome = regenerate(tmp_path, "notes", never_bound)

    assert problems == ("notes: system/supervisord.conf.d/notes.conf differs from what its manifest generates",)
    assert outcome.changed_paths == ("system/supervisord.conf.d/notes.conf",)
    assert conf.read_text() == generated
    assert check(tmp_path) == ()


def test_regenerate_follows_a_manifest_that_gained_a_service_and_keeps_the_port(tmp_path: Path) -> None:
    package = write_user_app(tmp_path, "notes")
    add_app(tmp_path, package, 8093, never_bound)
    manifest = tmp_path / "system" / "apps" / package / "app.toml"
    manifest.write_text(manifest.read_text() + '\n[[services]]\nrole = "indexer"\ncommand = "notes-indexer"\n')

    outcome = regenerate(tmp_path, None, never_bound)

    assert outcome.changed_paths == ("system/supervisord.conf.d/notes-indexer.conf",)
    assert outcome.port_by_reassigned_name == {}
    assert load_record(tmp_path, "notes").port == 8093


def test_two_apps_on_one_port_are_reported_and_the_one_recorded_later_is_moved(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    # Two branches each allocated 8080: the record committed first keeps it, whatever the names' order.
    for name, committer_date in (("zebra", "2026-01-01T00:00:00+00:00"), ("alpha", "2026-01-02T00:00:00+00:00")):
        package = write_user_app(tmp_path, name)
        write_record(tmp_path, AppRecord(name=name, package=package, port=8080, origin=AppOrigin.LOCAL))
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", f"add {name}", committer_date=committer_date)

    before = check(tmp_path)
    outcome = regenerate(tmp_path, None, never_bound)

    assert "port 8080 is recorded for more than one app: alpha, zebra" in before
    assert outcome.port_by_reassigned_name == {"alpha": 8081}
    assert load_record(tmp_path, "zebra").port == 8080
    assert load_record(tmp_path, "alpha").port == 8081
    assert check(tmp_path) == ()


@pytest.mark.parametrize(
    ("existing_name", "added_name"),
    [
        pytest.param("notes", "notes-sync", id="the-new-name-extends-an-existing-one"),
        pytest.param("notes-sync", "notes", id="an-existing-name-extends-the-new-one"),
    ],
)
def test_an_app_named_as_another_apps_background_program_would_be_is_refused(
    tmp_path: Path, existing_name: str, added_name: str
) -> None:
    existing_package = write_user_app(tmp_path, existing_name)
    add_app(tmp_path, existing_package, None, never_bound)
    added_package = write_user_app(tmp_path, added_name)

    with pytest.raises(CollisionError) as raised:
        add_app(tmp_path, added_package, None, never_bound)

    assert raised.value.problems == (
        f"the name {added_name!r} and the name {existing_name!r} of the app at system/apps/{existing_package} "
        "differ only by a hyphenated suffix, which is how an app's background programs are named",
    )
    assert not record_path(tmp_path, added_name).exists()


def test_two_apps_whose_names_only_begin_alike_are_both_added(tmp_path: Path) -> None:
    add_app(tmp_path, write_user_app(tmp_path, "notes"), None, never_bound)

    add_app(tmp_path, write_user_app(tmp_path, "notebook"), None, never_bound)

    assert record_path(tmp_path, "notebook").is_file()


def test_regenerating_an_app_that_has_no_record_is_refused_before_any_port_is_moved(tmp_path: Path) -> None:
    for name in ("alpha", "zebra"):
        package = write_user_app(tmp_path, name)
        write_record(tmp_path, AppRecord(name=name, package=package, port=8080, origin=AppOrigin.LOCAL))

    with pytest.raises(AppNotFoundError, match="nowhere"):
        regenerate(tmp_path, "nowhere", never_bound)

    assert [load_record(tmp_path, name).port for name in ("alpha", "zebra")] == [8080, 8080]


def test_regenerating_an_app_whose_new_skill_the_workspace_already_holds_is_refused_with_nothing_written(
    tmp_path: Path,
) -> None:
    for name in ("alpha", "zebra"):
        package = write_user_app(tmp_path, name)
        write_record(tmp_path, AppRecord(name=name, package=package, port=8080, origin=AppOrigin.LOCAL))
    write_workspace_file(tmp_path, ".agents/skills/weekly-review/SKILL.md", "# The workspace's own\n")
    write_app_file(tmp_path, "zebra", "skills/weekly-review/SKILL.md", "# The app's\n")

    with pytest.raises(CollisionError) as raised:
        regenerate(tmp_path, None, never_bound)

    assert raised.value.problems == (
        "zebra: the skill 'weekly-review' already exists at .agents/skills/weekly-review",
    )
    assert (tmp_path / ".agents" / "skills" / "weekly-review" / "SKILL.md").read_text() == "# The workspace's own\n"
    assert [load_record(tmp_path, name).port for name in ("alpha", "zebra")] == [8080, 8080]
    assert not (tmp_path / "system" / "supervisord.conf.d").exists()


def test_regenerating_never_takes_the_link_to_another_apps_skill(tmp_path: Path) -> None:
    alpha = write_user_app(tmp_path, "alpha")
    write_app_file(tmp_path, alpha, "skills/shared-notes/SKILL.md")
    add_app(tmp_path, alpha, None, never_bound)
    zebra = write_user_app(tmp_path, "zebra")
    add_app(tmp_path, zebra, None, never_bound)
    link = tmp_path / ".agents" / "skills" / "shared-notes"

    write_app_file(tmp_path, zebra, "skills/shared-notes/SKILL.md")
    with pytest.raises(CollisionError) as raised:
        regenerate(tmp_path, "zebra", never_bound)
    (tmp_path / "system" / "apps" / zebra / "skills" / "shared-notes" / "SKILL.md").unlink()
    write_app_file(tmp_path, alpha, "skills/brand-new/SKILL.md")
    write_app_file(tmp_path, zebra, "skills/brand-new/SKILL.md")
    with pytest.raises(CollisionError) as raised_for_a_new_name:
        regenerate(tmp_path, None, never_bound)

    assert raised.value.problems == ("zebra: the skill 'shared-notes' already exists at .agents/skills/shared-notes",)
    assert os.readlink(link) == "../../system/apps/alpha/skills/shared-notes"
    assert raised_for_a_new_name.value.problems == (
        "zebra: .agents/skills/brand-new is also the link to a skill of alpha",
    )
    assert not (tmp_path / ".agents" / "skills" / "brand-new").is_symlink()


def test_an_installed_app_with_a_key_this_workspace_does_not_know_still_regenerates(tmp_path: Path) -> None:
    package = write_user_app(tmp_path, "notes", "\nfuture_flag = true\n")
    write_record(tmp_path, AppRecord(name="notes", package=package, port=8084, origin=AppOrigin.INSTALLED))

    outcome = regenerate(tmp_path, "notes", never_bound)

    assert outcome.changed_paths == ("system/supervisord.conf.d/notes.conf",)
    assert check(tmp_path) == ()
    assert [(app.record.name, app.state) for app in list_apps(tmp_path)] == [("notes", "installed")]


def test_an_owned_app_with_an_unknown_key_is_a_problem_check_names(tmp_path: Path) -> None:
    package = write_user_app(tmp_path, "notes", "\nfuture_flag = true\n")
    write_record(tmp_path, AppRecord(name="notes", package=package, port=8084, origin=AppOrigin.LOCAL))

    problems = check(tmp_path)

    assert len(problems) == 1
    assert problems[0].startswith("notes: ")
    assert "future_flag" in problems[0]


def test_a_record_whose_directory_is_gone_is_a_problem_check_names(tmp_path: Path) -> None:
    write_record(tmp_path, AppRecord(name="notes", package="notes", port=8084, origin=AppOrigin.LOCAL))

    assert check(tmp_path) == ("notes: no app at system/apps/notes",)


def test_the_command_says_what_it_did_in_one_json_line_and_exits_non_zero_when_refused(tmp_path: Path) -> None:
    runner = CliRunner()
    package = write_user_app(tmp_path, "notes")
    root = ["--repo-root", str(tmp_path)]

    added = runner.invoke(main, [*root, "add", package, "--port", "8093"])
    again = runner.invoke(main, [*root, "add", package])
    listed = runner.invoke(main, [*root, "list"])
    consistent = runner.invoke(main, [*root, "check"])
    (tmp_path / "system" / "supervisord.conf.d" / "notes.conf").unlink()
    inconsistent = runner.invoke(main, [*root, "check"])
    regenerated = runner.invoke(main, [*root, "regenerate"])
    unknown = runner.invoke(main, [*root, "regenerate", "digest"])
    missing = runner.invoke(main, [*root, "add", "digest"])

    assert (added.exit_code, json.loads(added.output)) == (
        0,
        {
            "result": "added",
            "name": "notes",
            "package": "notes",
            "port": 8093,
            "changed_paths": ["system/config/apps/notes.toml", "system/supervisord.conf.d/notes.conf"],
        },
    )
    assert (again.exit_code, json.loads(again.output)["result"]) == (1, "refused")
    assert json.loads(unknown.output) == {"result": "refused", "detail": "no app record for 'digest'"}
    assert json.loads(missing.output) == {
        "result": "refused",
        "package": "digest",
        "detail": "no app at system/apps/digest",
    }
    assert [app["name"] for app in json.loads(listed.output)["apps"]] == ["notes"]
    assert json.loads(listed.output)["apps"][0]["state"] == "owned"
    assert (consistent.exit_code, json.loads(consistent.output)) == (0, {"result": "consistent"})
    assert (inconsistent.exit_code, json.loads(inconsistent.output)) == (
        1,
        {"result": "inconsistent", "problems": ["notes: system/supervisord.conf.d/notes.conf is missing"]},
    )
    assert json.loads(regenerated.output) == {
        "result": "regenerated",
        "changed_paths": ["system/supervisord.conf.d/notes.conf"],
        "reassigned_ports": {},
    }


def test_a_collision_is_reported_with_every_problem_by_the_command(tmp_path: Path) -> None:
    write_workspace_file(tmp_path, "system/supervisord.conf.d/notes.conf", "[program:notes]\ncommand=other\n")
    package = write_user_app(tmp_path, "notes")

    result = CliRunner().invoke(main, ["--repo-root", str(tmp_path), "add", package])

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "result": "collision",
        "package": "notes",
        "problems": ["the program 'notes' is already declared in system/supervisord.conf.d/notes.conf"],
    }


def test_a_regenerate_that_would_take_a_skill_something_else_holds_is_reported_as_a_collision(tmp_path: Path) -> None:
    add_app(tmp_path, write_user_app(tmp_path, "notes"), None, never_bound)
    write_workspace_file(tmp_path, ".agents/skills/weekly-review/SKILL.md")
    write_app_file(tmp_path, "notes", "skills/weekly-review/SKILL.md")

    result = CliRunner().invoke(main, ["--repo-root", str(tmp_path), "regenerate", "notes"])

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "result": "collision",
        "problems": ["notes: the skill 'weekly-review' already exists at .agents/skills/weekly-review"],
    }


_NOTES_WITH_A_SERVICE_AND_A_SECRET = (
    '\n[[services]]\nrole = "indexer"\ncommand = "notes-indexer"\n'
    '\n[[secrets]]\nfile = "notes"\nvariables = ["NOTES_API_KEY"]\n'
)


def test_uninstalling_removes_the_app_and_what_was_generated_for_it_and_keeps_its_data(tmp_path: Path) -> None:
    package = write_user_app(tmp_path, "notes", _NOTES_WITH_A_SERVICE_AND_A_SECRET)
    write_app_file(tmp_path, package, "skills/weekly-review/SKILL.md", "# Weekly review\n")
    add_app(tmp_path, package, None, never_bound)
    add_app(tmp_path, write_user_app(tmp_path, "digest"), None, never_bound)
    write_workspace_file(tmp_path, "data/.apps/notes/notes.json", "[]")
    write_workspace_file(tmp_path, "data/.secrets/notes.env", "NOTES_API_KEY='k'\n")
    runtime = RecordingRuntime(repo_root=tmp_path)

    outcome = uninstall_app(tmp_path, "notes", runtime)

    assert sorted(outcome.removed_paths) == [
        ".agents/skills/weekly-review",
        "system/apps/notes",
        "system/config/apps/notes.toml",
        "system/supervisord.conf.d/notes-indexer.conf",
        "system/supervisord.conf.d/notes.conf",
    ]
    assert all(not (tmp_path / path).exists() and not (tmp_path / path).is_symlink() for path in outcome.removed_paths)
    assert outcome.kept_paths == ("data/.apps/notes", "data/.secrets/notes.env")
    assert (tmp_path / "data" / ".apps" / "notes" / "notes.json").read_text() == "[]"
    assert outcome.problems == ()
    assert runtime.calls == [
        "reload_programs",
        "remove_registry_row notes",
        "delete_ref refs/app-upstream/notes",
        "delete_ref refs/app-published/notes",
        f"commit_paths {sorted(outcome.removed_paths)} 'app-install: uninstall notes'",
    ]
    # Nothing else of the app was on this machine or on GitHub, and nothing else in the workspace names it.
    assert outcome.removed_state_paths == ()
    assert outcome.kept_share_grants is None
    assert outcome.kept_routine_state == ()
    assert outcome.kept_repositories == ()
    assert outcome.dependents == ()
    # The neighbour is untouched and the tree is still consistent.
    assert (tmp_path / "system" / "supervisord.conf.d" / "digest.conf").is_file()
    assert check(tmp_path) == ()


def test_an_uninstall_that_cannot_reach_the_running_workspace_still_removes_the_app_and_says_what_it_could_not_do(
    tmp_path: Path,
) -> None:
    add_app(tmp_path, write_user_app(tmp_path, "notes"), None, never_bound)
    runtime = RecordingRuntime(
        repo_root=tmp_path,
        reload_failure="unix:///var/run/supervisor.sock no such file",
        registry_failure="no row named notes",
        ref_failure="fatal: not a git repository",
        commit_failure="fatal: not a git repository",
    )

    outcome = uninstall_app(tmp_path, "notes", runtime)

    assert not (tmp_path / "system" / "apps" / "notes").exists()
    assert outcome.kept_paths == ()
    assert outcome.problems == (
        "supervisord did not drop the app's programs: unix:///var/run/supervisor.sock no such file",
        "the app's registry row was not removed: no row named notes",
        "the app's upstream ref was not deleted: fatal: not a git repository",
        "the app's published ref was not deleted: fatal: not a git repository",
        "the removal was not committed: fatal: not a git repository",
    )


def test_an_uninstall_names_the_grants_and_routine_state_a_later_app_of_the_name_inherits(tmp_path: Path) -> None:
    package = write_user_app(tmp_path, "notes")
    write_app_file(tmp_path, package, "routines/notes-digest.toml", 'id = "notes-digest"\n')
    write_app_file(tmp_path, package, "routines/notes-cleanup.toml", 'id = "notes-cleanup"\n')
    add_app(tmp_path, package, None, never_bound)
    write_workspace_file(
        tmp_path,
        "data/.secrets/share_grants.toml",
        "[workspace]\nusers = []\nemails = []\nemail_domains = []\n"
        '\n[services.notes]\nusers = ["3f1c"]\nemails = ["carol@example.com"]\nemail_domains = ["example.org"]\n'
        '\n[services.digest]\nusers = ["9a2b"]\nemails = []\nemail_domains = []\n',
    )
    # Only the digest routine has run on this machine.
    write_workspace_file(tmp_path, "data/.state/routines/notes-digest/state.json", '{"is_enabled": false}')
    write_workspace_file(tmp_path, "data/.state/routines/other-routine/state.json", "{}")

    outcome = uninstall_app(tmp_path, "notes", RecordingRuntime(repo_root=tmp_path))

    assert outcome.kept_share_grants is not None
    assert outcome.kept_share_grants.model_dump() == {
        "path": "data/.secrets/share_grants.toml",
        "table": "services.notes",
        "grant_count": 3,
    }
    assert outcome.kept_routine_state == ("data/.state/routines/notes-digest",)
    assert outcome.problems == ()
    # Named, never deleted: the next app called notes takes both over.
    assert "[services.notes]" in (tmp_path / "data" / ".secrets" / "share_grants.toml").read_text()
    assert (tmp_path / "data" / ".state" / "routines" / "notes-digest" / "state.json").is_file()


def test_an_uninstall_reports_a_grants_file_it_cannot_read_and_an_app_shared_with_nobody(tmp_path: Path) -> None:
    add_app(tmp_path, write_user_app(tmp_path, "notes"), None, never_bound)
    add_app(tmp_path, write_user_app(tmp_path, "digest"), None, never_bound)
    grants_file = write_workspace_file(tmp_path, "data/.secrets/share_grants.toml", "[services.notes\n")

    unreadable = uninstall_app(tmp_path, "notes", RecordingRuntime(repo_root=tmp_path))

    assert unreadable.kept_share_grants is None
    assert len(unreadable.problems) == 1
    assert unreadable.problems[0].startswith("whether notes was shared with anyone could not be read:")

    grants_file.write_text("[workspace]\n\n[services.digest]\nusers = []\nemails = []\nemail_domains = []\n")
    unshared = uninstall_app(tmp_path, "digest", RecordingRuntime(repo_root=tmp_path))

    assert unshared.kept_share_grants is None
    assert unshared.problems == ()


def test_an_uninstall_names_the_apps_that_depend_on_the_app_and_the_repositories_it_leaves(tmp_path: Path) -> None:
    add_app(tmp_path, write_user_app(tmp_path, "datalib"), None, never_bound)
    add_app(
        tmp_path,
        write_user_app(tmp_path, "notes", '\n[[dependencies]]\nname = "datalib"\n'),
        None,
        never_bound,
    )
    add_app(tmp_path, write_user_app(tmp_path, "digest", '\n[[dependencies]]\nname = "notes"\n'), None, never_bound)
    # A built-in app, which has no record, can depend on the app too.
    write_user_app(tmp_path, "reports", '\n[[dependencies]]\nname = "datalib"\n')
    record = load_record(tmp_path, "datalib")
    write_record(
        tmp_path,
        record.model_copy_update(
            to_update(record.field_ref().dev_repo_url, "https://github.com/octo/datalib-dev"),
            to_update(record.field_ref().published_repo_url, "https://github.com/octo/datalib"),
        ),
    )

    outcome = uninstall_app(tmp_path, "datalib", RecordingRuntime(repo_root=tmp_path))

    assert outcome.dependents == ("notes", "reports")
    assert outcome.kept_repositories == ("https://github.com/octo/datalib-dev", "https://github.com/octo/datalib")
    # Dependencies are advisory: the apps that named it are still there.
    assert (tmp_path / "system" / "apps" / "notes" / "app.toml").is_file()


def test_an_uninstall_removes_what_this_machine_kept_about_the_app(tmp_path: Path) -> None:
    add_app(tmp_path, write_user_app(tmp_path, "notes"), None, never_bound)
    add_app(tmp_path, write_user_app(tmp_path, "digest"), None, never_bound)
    write_workspace_file(tmp_path, "data/.state/app_sync/notes.json", "{}")
    write_workspace_file(tmp_path, "data/.state/app_sync/digest.json", "{}")
    write_workspace_file(tmp_path, "data/.state/app_install/publish/notes.json", "{}")
    write_workspace_file(tmp_path, "data/.state/app_install/snapshots/notes/abc123/notes.json", "[]")

    outcome = uninstall_app(tmp_path, "notes", RecordingRuntime(repo_root=tmp_path))

    assert sorted(outcome.removed_state_paths) == [
        "data/.state/app_install/publish/notes.json",
        "data/.state/app_sync/notes.json",
    ]
    assert all(not (tmp_path / path).exists() for path in outcome.removed_state_paths)
    # The copy of the app's data an update made is the user's data, and another app's state is not this app's.
    assert outcome.kept_paths == ("data/.state/app_install/snapshots/notes",)
    assert (tmp_path / "data" / ".state" / "app_sync" / "digest.json").is_file()
    assert outcome.problems == ()


def test_an_app_that_is_not_registered_cannot_be_uninstalled(tmp_path: Path) -> None:
    with pytest.raises(AppRecordNotFoundError, match="notes"):
        uninstall_app(tmp_path, "notes", RecordingRuntime(repo_root=tmp_path))


def test_the_real_runtime_commits_exactly_the_removal_under_the_installers_own_author(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    package = write_user_app(workspace, "notes")
    add_app(workspace, package, None, never_bound)
    commit_all(workspace, "a workspace with notes")
    # An agent's shell exports its own identity, which git reads ahead of any configuration.
    for variable in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(variable, "an agent")
    for variable in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(variable, "agent@workspace.invalid")
    # Someone else's uncommitted work, which the removal must not sweep into its commit.
    write_workspace_file(workspace, "README.md", "the workspace, edited\n")

    outcome = uninstall_app(workspace, "notes", WorkspaceRuntime(repo_root=workspace))

    log = run_git(workspace, ["log", "-1", "--format=%an <%ae>%n%s", "--name-only"]).splitlines()
    assert log[:2] == ["app-install <app-install@workspace.invalid>", "app-install: uninstall notes"]
    assert sorted(line for line in log[2:] if line) == [
        "system/apps/notes/app.toml",
        "system/apps/notes/icon.svg",
        "system/apps/notes/pyproject.toml",
        "system/config/apps/notes.toml",
        "system/supervisord.conf.d/notes.conf",
    ]
    assert run_git(workspace, ["status", "--porcelain"]) == "M README.md"
    # No supervisord runs beside a bare repository, which is reported and does not stop the removal; an app
    # with no registry row has none to remove.
    assert [problem.split(":")[0] for problem in outcome.problems] == ["supervisord did not drop the app's programs"]


def test_the_command_says_what_an_uninstall_removed_and_what_it_left(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    add_app(workspace, write_user_app(workspace, "notes"), None, never_bound)
    record = load_record(workspace, "notes")
    write_record(
        workspace,
        record.model_copy_update(to_update(record.field_ref().dev_repo_url, "https://github.com/octo/notes-dev")),
    )
    commit_all(workspace, "a workspace with notes")
    write_workspace_file(workspace, "data/.apps/notes/notes.json", "[]")
    root = ["--repo-root", str(workspace)]

    uninstalled = CliRunner().invoke(main, [*root, "uninstall", "notes"])
    again = CliRunner().invoke(main, [*root, "uninstall", "notes"])

    report = json.loads(uninstalled.output)
    (problem,) = report.pop("problems")
    assert problem.startswith("supervisord did not drop the app's programs")
    assert (uninstalled.exit_code, report) == (
        0,
        {
            "result": "uninstalled",
            "name": "notes",
            "removed_paths": [
                "system/supervisord.conf.d/notes.conf",
                "system/apps/notes",
                "system/config/apps/notes.toml",
            ],
            "removed_state_paths": [],
            "kept_paths": ["data/.apps/notes"],
            "kept_share_grants": None,
            "kept_routine_state": [],
            "kept_repositories": ["https://github.com/octo/notes-dev"],
            "dependents": [],
        },
    )
    assert (again.exit_code, json.loads(again.output)) == (
        1,
        {
            "result": "refused",
            "name": "notes",
            "detail": "no app record for 'notes' (expected system/config/apps/notes.toml)",
        },
    )


def test_the_real_runtime_commits_a_removal_that_holds_a_file_no_commit_had(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    package = write_user_app(workspace, "notes")
    add_app(workspace, package, None, never_bound)
    commit_all(workspace, "a workspace with notes")
    # The app gained a skill, and its link was generated but never committed.
    write_app_file(workspace, package, "skills/weekly-review/SKILL.md", "# Weekly review\n")
    regenerate(workspace, "notes", never_bound)

    outcome = uninstall_app(workspace, "notes", WorkspaceRuntime(repo_root=workspace))

    assert ".agents/skills/weekly-review" in outcome.removed_paths
    assert not any(problem.startswith("the removal was not committed") for problem in outcome.problems)
    assert run_git(workspace, ["log", "-1", "--format=%s"]) == "app-install: uninstall notes"
    assert run_git(workspace, ["status", "--porcelain"]) == ""


def test_the_real_runtime_has_nothing_to_commit_for_an_app_no_commit_held(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    add_app(workspace, write_user_app(workspace, "notes"), None, never_bound)
    before = run_git(workspace, ["rev-parse", "HEAD"])

    outcome = uninstall_app(workspace, "notes", WorkspaceRuntime(repo_root=workspace))

    assert not any(problem.startswith("the removal was not committed") for problem in outcome.problems)
    assert run_git(workspace, ["rev-parse", "HEAD"]) == before
    assert run_git(workspace, ["status", "--porcelain"]) == ""
