import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from app_install.cli import main
from app_install.errors import AppAlreadyRegisteredError
from app_install.errors import AppNotFoundError
from app_install.errors import CollisionError
from app_install.errors import PortUnavailableError
from app_install.operations import add_app
from app_install.operations import check
from app_install.operations import list_apps
from app_install.operations import regenerate
from app_install.records import AppOrigin
from app_install.records import AppRecord
from app_install.records import load_record
from app_install.records import write_record
from app_install.testing import never_bound
from app_install.testing import write_app_file
from app_install.testing import write_user_app
from app_install.testing import write_workspace_file


@pytest.fixture(autouse=True)
def _no_registry_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDS_APPS_FILE", raising=False)


def _git(repo_root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *arguments],
        cwd=repo_root,
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
    # Two branches each allocated 8080: the record committed first keeps it.
    for name in ("zebra", "alpha"):
        package = write_user_app(tmp_path, name)
        write_record(tmp_path, AppRecord(name=name, package=package, port=8080, origin=AppOrigin.LOCAL))
        _git(tmp_path, "add", "-A")
        _git(
            tmp_path,
            "-c",
            f"user.name={name}",
            "commit",
            "-q",
            "-m",
            f"add {name}",
            "--date",
            "2026-01-01T00:00:00" if name == "zebra" else "2026-01-02T00:00:00",
        )

    before = check(tmp_path)
    outcome = regenerate(tmp_path, None, never_bound)

    assert "port 8080 is recorded for more than one app: alpha, zebra" in before
    assert outcome.port_by_reassigned_name == {"alpha": 8081} or outcome.port_by_reassigned_name == {"zebra": 8081}
    assert sorted(record.port for record in (load_record(tmp_path, "alpha"), load_record(tmp_path, "zebra"))) == [
        8080,
        8081,
    ]
    assert check(tmp_path) == ()


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

    assert check(tmp_path) == ("notes: 'no app at system/apps/notes'",)


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
