from pathlib import Path

import pytest
from inline_snapshot import snapshot

from app_manifest.errors import AppRecordNotFoundError
from app_manifest.errors import RecordLoadError
from app_manifest.records import AppOrigin
from app_manifest.records import AppRecord
from app_manifest.records import RecordReference
from app_manifest.records import load_all_records
from app_manifest.records import load_record
from app_manifest.records import parse_record
from app_manifest.records import render_record
from app_manifest.records import write_record


def _installed_record() -> AppRecord:
    return AppRecord(
        name="notes",
        package="notes",
        port=8084,
        origin=AppOrigin.INSTALLED,
        upstream_url="https://github.com/alice/notes",
        upstream_commit="48bd27357c1777cbca75c53a9dc7549b5027d89c",
        upstream_tree="6b1f0c0d2a8e5b7a3c4d5e6f708192a3b4c5d6e7",
        upstream_version="0.3.0",
        verified_template="minds-v0.11.0",
        references=(
            RecordReference(path=".agents/skills/weekly-review", note='Calls GET /api/notes for the "weekly" summary'),
        ),
    )


def test_a_record_renders_as_the_same_text_every_time() -> None:
    assert render_record(_installed_record()) == snapshot("""\
# This app's record, kept by app-install: which directory is the app, its port, and where it came from.
# Change it with the app-install command rather than by hand.
name = "notes"
package = "notes"
port = 8084
origin = "installed"
upstream_url = "https://github.com/alice/notes"
upstream_commit = "48bd27357c1777cbca75c53a9dc7549b5027d89c"
upstream_tree = "6b1f0c0d2a8e5b7a3c4d5e6f708192a3b4c5d6e7"
upstream_version = "0.3.0"
dev_repo_url = ""
published_repo_url = ""
published_commit = ""
published_version = ""
verified_template = "minds-v0.11.0"

[[references]]
path = ".agents/skills/weekly-review"
note = "Calls GET /api/notes for the \\"weekly\\" summary"
""")


def test_a_record_reads_back_as_the_record_that_was_written(tmp_path: Path) -> None:
    record = _installed_record()

    written_path = write_record(tmp_path, record)

    assert written_path == tmp_path / "system" / "config" / "apps" / "notes.toml"
    assert load_record(tmp_path, "notes") == record
    assert load_all_records(tmp_path) == (record,)


def test_a_workspace_with_no_records_directory_has_no_user_apps(tmp_path: Path) -> None:
    assert load_all_records(tmp_path) == ()
    with pytest.raises(AppRecordNotFoundError, match="notes") as raised:
        load_record(tmp_path, "notes")
    assert str(raised.value) == "no app record for 'notes' (expected system/config/apps/notes.toml)"


def test_records_are_listed_by_name(tmp_path: Path) -> None:
    for name, port in (("zebra", 8081), ("alpha", 8080)):
        write_record(tmp_path, AppRecord(name=name, package=name, port=port, origin=AppOrigin.LOCAL))

    assert [str(record.name) for record in load_all_records(tmp_path)] == ["alpha", "zebra"]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        pytest.param("name = [", "not valid TOML", id="not-toml"),
        pytest.param('name = "notes"\npackage = "notes"\nport = 8084\n', "origin", id="missing-origin"),
        pytest.param(
            'name = "notes"\npackage = "notes"\nport = 8084\norigin = "borrowed"\n', "origin", id="unknown-origin"
        ),
        pytest.param(
            'name = "notes"\npackage = "notes"\nport = 0\norigin = "local"\n', "port", id="port-out-of-range"
        ),
        pytest.param(
            'name = "notes"\npackage = "notes"\nport = 8084\norigin = "local"\ncolour = "red"\n',
            "colour",
            id="unknown-key",
        ),
        pytest.param('name = "Not A Name"\npackage = "notes"\nport = 8084\norigin = "local"\n', "name", id="bad-name"),
    ],
)
def test_a_file_that_is_not_a_record_is_refused_naming_what_is_wrong(text: str, problem: str) -> None:
    with pytest.raises(RecordLoadError, match=problem):
        parse_record(text, Path("system/config/apps/notes.toml"))


def test_a_record_in_a_file_named_after_another_app_is_refused() -> None:
    text = render_record(AppRecord(name="notes", package="notes", port=8084, origin=AppOrigin.LOCAL))

    with pytest.raises(RecordLoadError, match="named after"):
        parse_record(text, Path("system/config/apps/digest.toml"))
