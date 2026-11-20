import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from app_install.cli import main
from app_install.errors import AppAlreadyRegisteredError
from app_install.errors import AppNotFoundError
from app_install.migration import migrate_app
from app_install.operations import add_app
from app_install.operations import check
from app_install.testing import never_bound
from app_install.testing import write_app_file
from app_install.testing import write_user_app
from app_install.testing import write_workspace_file
from app_manifest.manifest import ManifestReadMode
from app_manifest.manifest import load_manifest
from app_manifest.records import AppOrigin
from app_manifest.records import AppRecord
from app_manifest.records import RecordReference
from app_manifest.records import load_record

# The program file the scaffold wrote for an app before program files were generated.
_HAND_WRITTEN_CONF = """\
[program:{name}]
command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "python3 system/scripts/workspace_bare_scripts/venv_links.py ensure-project system/apps/{package} && uv sync --locked --project system/apps/{package} && python3 system/scripts/forward_port.py --manifest system/apps/{package}/app.toml --url http://localhost:{port} && exec {entry_point}"
directory=/home/user/workspace
autostart=true
autorestart=true
startsecs=30
startretries=5
stopasgroup=true
killasgroup=true
stdout_logfile=/var/log/supervisor/{name}-stdout.log
stderr_logfile=/var/log/supervisor/{name}-stderr.log
stdout_logfile_maxbytes=10MB
stderr_logfile_maxbytes=10MB
stdout_logfile_backups=3
stderr_logfile_backups=3
"""

_OLD_LAYOUT_TABLES = """
# What the app owns outside its folder.
[[references]]
path = ".agents/skills/weekly-review"
note = "Drives the weekly summary"

[[references]]
path = ".agents/skills/notes-export"
note = "Exports notes through its own Python project"

[[references]]
path = "docs/notes.md"
note = "Describes the notes format"

[scope]
exclude = ["system/apps/notes/fixtures/**", ".agents/skills/weekly-review/samples/**", "docs/drafts/**"]
"""


@pytest.fixture(autouse=True)
def _no_registry_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDS_APPS_FILE", raising=False)


def _write_old_layout_app(repo_root: Path, entry_point: str = "system/apps/notes/.venv/bin/notes") -> str:
    """An app as the scaffold and an agent left it before apps kept everything in their directory."""
    package = write_user_app(repo_root, "notes", _OLD_LAYOUT_TABLES)
    write_workspace_file(
        repo_root,
        "system/supervisord.conf.d/notes.conf",
        _HAND_WRITTEN_CONF.format(name="notes", package=package, port=8087, entry_point=entry_point),
    )
    write_workspace_file(repo_root, ".agents/skills/weekly-review/SKILL.md", "# Weekly review\n")
    write_workspace_file(repo_root, ".agents/skills/weekly-review/samples/one.md", "a sample\n")
    write_workspace_file(repo_root, ".agents/skills/notes-export/SKILL.md", "# Export\n")
    write_workspace_file(repo_root, ".agents/skills/notes-export/python/pyproject.toml", "[project]\nname = 'x'\n")
    write_workspace_file(repo_root, "docs/notes.md", "# Notes format\n")
    write_workspace_file(repo_root, "system/routines/notes-nightly.toml", 'title = "Tidy notes"\napp = "notes"\n')
    write_workspace_file(repo_root, "system/routines/morning-digest.toml", 'title = "Digest"\napp = "digest"\n')
    return package


def _tree(repo_root: Path) -> dict[str, str]:
    """Every file and link of the tree with what it holds, for telling whether anything changed."""
    return {
        path.relative_to(repo_root).as_posix(): (
            f"-> {path.readlink()}" if path.is_symlink() else path.read_text(encoding="utf-8")
        )
        for path in sorted(repo_root.rglob("*"))
        if path.is_symlink() or path.is_file()
    }


def test_converting_an_app_gathers_what_it_owns_into_its_directory_and_records_the_rest(tmp_path: Path) -> None:
    package = _write_old_layout_app(tmp_path)

    outcome = migrate_app(tmp_path, package, never_bound, is_dry_run=False)

    assert (outcome.is_converted, outcome.problems) == (True, ())
    assert outcome.plan.moves == (
        (".agents/skills/weekly-review", "system/apps/notes/skills/weekly-review"),
        ("system/routines/notes-nightly.toml", "system/apps/notes/routines/notes-nightly.toml"),
    )
    assert outcome.plan.notes == ("the scope exclusion 'docs/drafts/**' names nothing inside the app and is dropped",)
    # The record keeps the port the app already ran on, and lists what still sits outside the app.
    assert load_record(tmp_path, "notes") == AppRecord(
        name="notes",
        package="notes",
        port=8087,
        origin=AppOrigin.LOCAL,
        references=(
            RecordReference(path=".agents/skills/notes-export", note="Exports notes through its own Python project"),
            RecordReference(path="docs/notes.md", note="Describes the notes format"),
        ),
    )
    # The skill is found where it always was, through a link, and now lives in the app.
    link = tmp_path / ".agents" / "skills" / "weekly-review"
    assert link.is_symlink()
    assert (link / "SKILL.md").read_text() == "# Weekly review\n"
    assert (tmp_path / "system" / "apps" / "notes" / "routines" / "notes-nightly.toml").is_file()
    assert (tmp_path / "system" / "routines" / "morning-digest.toml").is_file()
    assert not (tmp_path / "system" / "routines" / "notes-nightly.toml").exists()
    # A skill with a Python project of its own stays where the workspace's lock expects it.
    assert (tmp_path / ".agents" / "skills" / "notes-export" / "python" / "pyproject.toml").is_file()
    manifest_text = (tmp_path / "system" / "apps" / "notes" / "app.toml").read_text()
    manifest = load_manifest(tmp_path / "system" / "apps" / "notes" / "app.toml", mode=ManifestReadMode.STRICT)
    assert "[[references]]" not in manifest_text
    assert manifest.references == ()
    assert [str(glob) for glob in manifest.scope.exclude] == ["fixtures/**", "skills/weekly-review/samples/**"]
    conf = (tmp_path / "system" / "supervisord.conf.d" / "notes.conf").read_text()
    assert conf.startswith("# Generated by app-install from system/apps/notes/app.toml")
    assert "--url http://localhost:8087" in conf
    assert check(tmp_path) == ()


def test_a_dry_run_says_what_would_happen_and_changes_nothing(tmp_path: Path) -> None:
    package = _write_old_layout_app(tmp_path)
    before = _tree(tmp_path)

    outcome = migrate_app(tmp_path, package, never_bound, is_dry_run=True)

    assert (outcome.is_converted, outcome.problems, outcome.plan.port) == (False, (), 8087)
    assert len(outcome.plan.moves) == 2
    assert _tree(tmp_path) == before


def test_an_app_whose_program_file_is_not_what_its_manifest_generates_is_left_exactly_as_it_was(
    tmp_path: Path,
) -> None:
    # The program runs under a secret file the manifest does not declare.
    wrapped = "python3 system/scripts/with_secrets.py data/.secrets/notes.env -- system/apps/notes/.venv/bin/notes"
    package = _write_old_layout_app(tmp_path, entry_point=wrapped)
    before = _tree(tmp_path)

    outcome = migrate_app(tmp_path, package, never_bound, is_dry_run=False)

    assert outcome.is_converted is False
    assert len(outcome.problems) == 1
    assert outcome.problems[0].startswith(
        "system/supervisord.conf.d/notes.conf is not what the app's manifest generates"
    )
    assert "with_secrets.py data/.secrets/notes.env" in outcome.problems[0]
    assert _tree(tmp_path) == before


def test_declaring_how_the_app_runs_makes_the_same_app_convertible(tmp_path: Path) -> None:
    wrapped = "python3 system/scripts/with_secrets.py data/.secrets/notes.env -- system/apps/notes/.venv/bin/notes"
    package = _write_old_layout_app(tmp_path, entry_point=wrapped)
    manifest = tmp_path / "system" / "apps" / package / "app.toml"
    manifest.write_text(manifest.read_text() + '\n[[secrets]]\nfile = "notes"\nvariables = ["NOTES_API_KEY"]\n')

    outcome = migrate_app(tmp_path, package, never_bound, is_dry_run=False)

    assert (outcome.is_converted, outcome.problems) == (True, ())
    assert check(tmp_path) == ()


def test_a_conversion_that_fails_part_way_is_taken_back_step_by_step(tmp_path: Path) -> None:
    package = _write_old_layout_app(tmp_path)
    # Something that is not a directory sits where the records are kept, so writing the record fails after the
    # skill and the routine have moved and the manifest has been rewritten.
    write_workspace_file(tmp_path, "system/config/apps", "not a directory\n")
    before = _tree(tmp_path)

    outcome = migrate_app(tmp_path, package, never_bound, is_dry_run=False)

    assert outcome.is_converted is False
    assert len(outcome.problems) == 1
    assert outcome.problems[0].startswith("the conversion failed part-way")
    assert _tree(tmp_path) == before
    assert not (tmp_path / "system" / "apps" / "notes" / "skills").exists()
    assert not (tmp_path / "system" / "apps" / "notes" / "routines").exists()


def test_a_skill_another_app_also_names_stays_where_both_can_reach_it(tmp_path: Path) -> None:
    package = _write_old_layout_app(tmp_path)
    write_user_app(
        tmp_path, "digest", '\n[[references]]\npath = ".agents/skills/weekly-review"\nnote = "Reads the summary"\n'
    )

    outcome = migrate_app(tmp_path, package, never_bound, is_dry_run=False)

    assert outcome.is_converted is True
    assert (".agents/skills/weekly-review", "system/apps/notes/skills/weekly-review") not in outcome.plan.moves
    assert not (tmp_path / ".agents" / "skills" / "weekly-review").is_symlink()
    assert (
        RecordReference(path=".agents/skills/weekly-review", note="Drives the weekly summary")
        in load_record(tmp_path, "notes").references
    )


def test_an_app_with_no_program_file_gets_a_port_and_a_generated_one(tmp_path: Path) -> None:
    package = write_user_app(tmp_path, "notes")

    outcome = migrate_app(tmp_path, package, never_bound, is_dry_run=False)

    assert (outcome.is_converted, outcome.plan.port) == (True, 8080)
    assert (tmp_path / "system" / "supervisord.conf.d" / "notes.conf").is_file()
    assert check(tmp_path) == ()


def test_wiring_programs_keep_an_app_from_converting_until_they_are_services(tmp_path: Path) -> None:
    package = write_user_app(tmp_path, "notes", '\n[wiring]\nprograms = ["notes-display"]\n')
    write_workspace_file(tmp_path, "system/supervisord.conf.d/notes-display.conf", "[program:notes-display]\n")
    before = _tree(tmp_path)

    outcome = migrate_app(tmp_path, package, never_bound, is_dry_run=False)

    assert outcome.is_converted is False
    assert "[wiring] programs ['notes-display']" in outcome.problems[0]
    assert _tree(tmp_path) == before


def test_an_app_that_already_has_a_record_or_no_manifest_is_refused(tmp_path: Path) -> None:
    package = write_user_app(tmp_path, "notes")
    add_app(tmp_path, package, None, never_bound)

    with pytest.raises(AppAlreadyRegisteredError):
        migrate_app(tmp_path, package, never_bound, is_dry_run=False)
    with pytest.raises(AppNotFoundError):
        migrate_app(tmp_path, "missing", never_bound, is_dry_run=False)


def test_the_command_reports_a_conversion_and_an_app_it_left_alone(tmp_path: Path) -> None:
    package = _write_old_layout_app(tmp_path)
    write_app_file(tmp_path, "shelf", "app.toml", 'name = "shelf"\ndisplay_name = "Shelf"\nicon = "icon.svg"\n')
    write_app_file(tmp_path, "shelf", "icon.svg", "<svg/>")
    write_workspace_file(
        tmp_path, "system/supervisord.conf.d/shelf.conf", "[program:shelf]\ncommand=dufs --port 8089\n"
    )
    runner = CliRunner()
    root = ["--repo-root", str(tmp_path)]

    planned = runner.invoke(main, [*root, "migrate", package, "--dry-run"])
    migrated = runner.invoke(main, [*root, "migrate", package])
    unconverted = runner.invoke(main, [*root, "migrate", "shelf"])

    assert (planned.exit_code, json.loads(planned.output)["result"]) == (0, "would_migrate")
    report = json.loads(migrated.output)
    assert (migrated.exit_code, report["result"], report["name"], report["port"]) == (0, "migrated", "notes", 8087)
    assert report["moves"][0] == [".agents/skills/weekly-review", "system/apps/notes/skills/weekly-review"]
    assert [reference["path"] for reference in report["references"]] == [
        ".agents/skills/notes-export",
        "docs/notes.md",
    ]
    left_alone = json.loads(unconverted.output)
    assert (unconverted.exit_code, left_alone["result"]) == (1, "unconverted")
    assert "system/supervisord.conf.d/shelf.conf is not what the app's manifest generates" in left_alone["problems"][0]
