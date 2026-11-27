from datetime import datetime

import pytest
from inline_snapshot import snapshot
from pydantic import ValidationError

from app_manifest.records import record_path
from imbue.imbue_common.model_update import to_update
from routines.data_types import RoutineDefinition
from routines.data_types import ScheduleChange
from routines.definitions import FileDefinitionStore
from routines.errors import AppOwnedRoutineError
from routines.errors import DuplicateRoutineError
from routines.errors import RoutineExistsError
from routines.errors import RoutineNotFoundError
from routines.mock_repository_test import RecordingRepository
from routines.primitives import RoutineId
from routines.primitives import TimeOfDay
from routines.primitives import Weekday
from routines.testing import TEST_ROUTINE_ID
from routines.testing import make_user_app
from routines.testing import write_app_definition

_HOURLY_TOML = 'title = "Hourly"\nwhat = "Runs hourly."\nevery = "1h"\ncommand = ["true"]\n'


def _digest() -> RoutineDefinition:
    return RoutineDefinition.model_validate(
        {
            "title": "Morning inbox digest",
            "what": "Reads new mail.",
            "every": "1d",
            "at": "07:30",
            "skill": "inbox-digest",
        }
    )


def test_adding_a_routine_writes_its_file_without_the_defaults_and_commits_it(
    definitions: FileDefinitionStore, repository: RecordingRepository
) -> None:
    path = definitions.add(TEST_ROUTINE_ID, _digest())

    assert path.read_text() == snapshot("""\
title = "Morning inbox digest"
what = "Reads new mail."
every = "1d"
at = "07:30"
skill = "inbox-digest"
""")
    assert repository.commits == [(path, "routines: add inbox-digest")]
    assert definitions.load(TEST_ROUTINE_ID).definition == _digest()


def test_adding_a_routine_that_exists_is_refused(definitions: FileDefinitionStore) -> None:
    definitions.add(TEST_ROUTINE_ID, _digest())
    with pytest.raises(RoutineExistsError):
        definitions.add(TEST_ROUTINE_ID, _digest())


def test_replacing_a_routine_keeps_the_comments_an_agent_left(
    definitions: FileDefinitionStore, repository: RecordingRepository
) -> None:
    path = definitions.add(TEST_ROUTINE_ID, _digest())
    path.write_text("# The user asked for this on a Tuesday.\n" + path.read_text())

    definition = _digest()
    changed = definition.model_copy_update(
        to_update(definition.field_ref().at, TimeOfDay("08:00")),
        to_update(definition.field_ref().days, (Weekday.MON,)),
    )
    definitions.replace(TEST_ROUTINE_ID, changed, "change the schedule")

    assert path.read_text() == snapshot("""\
# The user asked for this on a Tuesday.
title = "Morning inbox digest"
what = "Reads new mail."
every = "1d"
at = "08:00"
skill = "inbox-digest"
days = ["mon"]
""")
    assert repository.commits[-1] == (path, "routines: change the schedule of inbox-digest")


def test_a_broken_file_is_listed_with_its_reason_and_never_raises(definitions: FileDefinitionStore) -> None:
    (definitions.paths.definitions_dir / "broken.toml").write_text(
        'title = "No what"\nevery = "1d"\ncommand = ["true"]\n'
    )
    (definitions.paths.definitions_dir / "garbled.toml").write_text("title = \n")

    listed = {loaded.routine_id: loaded for loaded in definitions.list_routines()}

    assert listed[RoutineId("broken")].definition is None
    assert listed[RoutineId("broken")].error == snapshot("what: Field required")
    garbled_error = listed[RoutineId("garbled")].error
    assert garbled_error is not None
    assert garbled_error.startswith("not valid TOML")


def test_a_file_whose_name_is_not_a_routine_id_is_skipped(definitions: FileDefinitionStore) -> None:
    definitions.add(TEST_ROUTINE_ID, _digest())
    (definitions.paths.definitions_dir / "Not An Id.toml").write_text('title = "x"\n')

    assert [loaded.routine_id for loaded in definitions.list_routines()] == [TEST_ROUTINE_ID]


def test_replacing_a_file_that_no_longer_parses_rewrites_it_whole(
    definitions: FileDefinitionStore, repository: RecordingRepository
) -> None:
    path = definitions.add(TEST_ROUTINE_ID, _digest())
    path.write_text("# a comment that will not survive\ntitle = \n")

    definitions.replace(TEST_ROUTINE_ID, _digest(), "change the schedule")

    assert definitions.load(TEST_ROUTINE_ID).definition == _digest()
    assert "a comment" not in path.read_text()
    assert repository.commits[-1] == (path, "routines: change the schedule of inbox-digest")


def test_routines_are_listed_from_the_workspace_and_from_every_app_in_one_fixed_order(
    definitions: FileDefinitionStore,
) -> None:
    paths = definitions.paths
    in_zeta = write_app_definition(paths, "zeta", RoutineId("alpha-report"), _HOURLY_TOML)
    own = paths.workspace_definition_path(RoutineId("beta-sync"))
    own.write_text(_HOURLY_TOML)
    in_inbox = write_app_definition(paths, "inbox_status", RoutineId("gamma-digest"), _HOURLY_TOML)

    listed = definitions.list_routines()

    assert [(loaded.routine_id, loaded.path, loaded.app_package) for loaded in listed] == [
        (RoutineId("alpha-report"), in_zeta, "zeta"),
        (RoutineId("beta-sync"), own, None),
        (RoutineId("gamma-digest"), in_inbox, "inbox_status"),
    ]
    assert all(loaded.definition is not None and not loaded.is_duplicated for loaded in listed)
    assert definitions.list_routines() == listed


def test_a_routine_an_app_owns_is_loaded_from_the_apps_directory(definitions: FileDefinitionStore) -> None:
    path = write_app_definition(definitions.paths, "inbox_status", TEST_ROUTINE_ID, _HOURLY_TOML)

    loaded = definitions.load(TEST_ROUTINE_ID)

    assert loaded.definition is not None
    assert loaded.definition.title == "Hourly"
    assert (loaded.path, loaded.app_package) == (path, "inbox_status")


def test_an_id_two_files_have_is_one_broken_routine_that_names_both_and_changes_neither(
    definitions: FileDefinitionStore, repository: RecordingRepository
) -> None:
    own = definitions.paths.workspace_definition_path(TEST_ROUTINE_ID)
    own.write_text(_HOURLY_TOML)
    in_app = write_app_definition(definitions.paths, "inbox_status", TEST_ROUTINE_ID, _HOURLY_TOML)

    listed = definitions.list_routines()

    assert [loaded.routine_id for loaded in listed] == [TEST_ROUTINE_ID]
    assert listed[0] == definitions.load(TEST_ROUTINE_ID)
    assert listed[0].definition is None
    assert listed[0].is_duplicated
    assert listed[0].error == snapshot(
        "defined in more than one file (system/routines/inbox-digest.toml, "
        "system/apps/inbox_status/routines/inbox-digest.toml); a routine id is unique across the workspace, so "
        "rename or delete all but one"
    )
    with pytest.raises(DuplicateRoutineError, match="more than one file"):
        definitions.replace(TEST_ROUTINE_ID, _digest(), "edit")
    with pytest.raises(DuplicateRoutineError, match="more than one file"):
        definitions.remove(TEST_ROUTINE_ID)
    assert own.read_text() == _HOURLY_TOML
    assert in_app.read_text() == _HOURLY_TOML
    assert repository.commits == []


def test_adding_a_routine_whose_id_an_app_already_has_is_refused(definitions: FileDefinitionStore) -> None:
    write_app_definition(definitions.paths, "inbox_status", TEST_ROUTINE_ID, _HOURLY_TOML)

    with pytest.raises(RoutineExistsError):
        definitions.add(TEST_ROUTINE_ID, _digest())

    assert not definitions.paths.workspace_definition_path(TEST_ROUTINE_ID).exists()


def test_a_routine_added_for_a_user_app_is_written_into_the_apps_directory_and_committed(
    definitions: FileDefinitionStore, repository: RecordingRepository
) -> None:
    make_user_app(definitions.paths, "inbox-status")
    definition = _digest().model_copy_update(to_update(_digest().field_ref().app, "inbox-status"))

    path = definitions.add(TEST_ROUTINE_ID, definition)

    assert path == definitions.paths.repo_root / "system/apps/inbox_status/routines/inbox-digest.toml"
    assert 'app = "inbox-status"' in path.read_text()
    assert repository.commits == [(path, "routines: add inbox-digest")]
    loaded = definitions.load(TEST_ROUTINE_ID)
    assert (loaded.definition, loaded.app_package) == (definition, "inbox_status")


def test_a_user_apps_directory_is_the_one_its_record_names_whatever_the_app_is_called(
    definitions: FileDefinitionStore,
) -> None:
    make_user_app(definitions.paths, "news", app_package="news_reader")
    # A directory that happens to carry the app's name belongs to something else.
    definitions.paths.app_dir("news").mkdir(parents=True)
    definition = _digest().model_copy_update(to_update(_digest().field_ref().app, "news"))

    path = definitions.add(TEST_ROUTINE_ID, definition)

    assert path == definitions.paths.repo_root / "system/apps/news_reader/routines/inbox-digest.toml"
    assert definitions.load(TEST_ROUTINE_ID).app_package == "news_reader"


@pytest.mark.parametrize("app_name", ["chat", "nothing-by-this-name", "../escape"])
def test_a_routine_added_for_an_app_that_is_no_user_app_stays_under_system_routines(
    definitions: FileDefinitionStore, app_name: str
) -> None:
    """A built-in app has a directory under system/apps and no record of an install; the other names have neither."""
    definitions.paths.app_dir("chat").mkdir(parents=True)
    definition = _digest().model_copy_update(to_update(_digest().field_ref().app, app_name))

    path = definitions.add(TEST_ROUTINE_ID, definition)

    assert path == definitions.paths.workspace_definition_path(TEST_ROUTINE_ID)
    assert definitions.load(TEST_ROUTINE_ID).app_package is None


@pytest.mark.parametrize(
    "record_text",
    [
        pytest.param("name = [not toml", id="a-record-that-does-not-load"),
        pytest.param(
            'name = "news"\npackage = "news_reader"\nport = 8084\norigin = "local"\n',
            id="a-record-whose-directory-is-gone",
        ),
    ],
)
def test_a_routine_added_for_an_app_whose_record_leads_to_no_directory_stays_under_system_routines(
    definitions: FileDefinitionStore, record_text: str
) -> None:
    record = record_path(definitions.paths.repo_root, "news")
    record.parent.mkdir(parents=True)
    record.write_text(record_text)
    definition = _digest().model_copy_update(to_update(_digest().field_ref().app, "news"))

    path = definitions.add(TEST_ROUTINE_ID, definition)

    assert path == definitions.paths.workspace_definition_path(TEST_ROUTINE_ID)
    assert definitions.load(TEST_ROUTINE_ID).app_package is None


def test_an_apps_file_is_never_rewritten_or_deleted(
    definitions: FileDefinitionStore, repository: RecordingRepository
) -> None:
    path = write_app_definition(definitions.paths, "inbox_status", TEST_ROUTINE_ID, _HOURLY_TOML)

    with pytest.raises(AppOwnedRoutineError, match="belongs to the app in system/apps/inbox_status/"):
        definitions.replace(TEST_ROUTINE_ID, _digest(), "edit")
    with pytest.raises(AppOwnedRoutineError, match="system/apps/inbox_status/routines/inbox-digest.toml"):
        definitions.remove(TEST_ROUTINE_ID)

    assert path.read_text() == _HOURLY_TOML
    assert repository.commits == []


def test_loading_a_routine_that_does_not_exist_raises(definitions: FileDefinitionStore) -> None:
    with pytest.raises(RoutineNotFoundError):
        definitions.load(RoutineId("nothing"))


def test_removing_a_routine_deletes_the_file_and_commits_the_deletion(
    definitions: FileDefinitionStore, repository: RecordingRepository
) -> None:
    path = definitions.add(TEST_ROUTINE_ID, _digest())
    definitions.remove(TEST_ROUTINE_ID)
    assert not path.exists()
    assert repository.commits[-1] == (path, "routines: remove inbox-digest")


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        (
            {"every": "1d", "once": datetime(2026, 11, 3, 15, 0), "command": ["true"]},
            "exactly one of 'every' and 'once'",
        ),
        ({"every": "1d", "command": ["true"], "skill": "x"}, "exactly one of 'command' and 'skill'"),
        ({"every": "1d", "command": []}, "at least one word"),
        ({"every": "15m", "at": "07:00", "command": ["true"]}, "a day or longer"),
        ({"every": "90s", "command": ["true"]}, "minutes, hours, days, weeks or months"),
        ({"every": "1d", "day_of_month": 3, "command": ["true"]}, "a cadence in months"),
        ({"once": datetime(2026, 11, 3, 15, 0), "at": "07:00", "command": ["true"]}, "go with 'every'"),
        ({"every": "1d", "timeout": "1mo", "command": ["true"]}, "fixed lengths"),
        ({"once": "2026-11-03T15:00:00Z", "command": ["true"]}, "without a zone"),
    ],
)
def test_a_definition_that_does_not_say_what_a_routine_is_is_refused_with_a_reason(
    fields: dict[str, object], problem: str
) -> None:
    with pytest.raises(ValidationError, match=problem):
        RoutineDefinition.model_validate({"title": "T", "what": "W", **fields})


def test_a_schedule_change_refuses_a_once_that_carries_a_zone_and_takes_a_local_one() -> None:
    """The file holds a local moment without a zone, so a zoned one would come back shifted by its offset."""
    with pytest.raises(ValidationError, match="without a zone"):
        ScheduleChange.model_validate({"once": "2026-11-03T15:00:00+02:00"})
    assert ScheduleChange.model_validate({"once": "2026-11-03T15:00"}).once == datetime(2026, 11, 3, 15, 0)


def test_weekdays_are_read_in_any_case() -> None:
    definition = RoutineDefinition.model_validate(
        {"title": "T", "what": "W", "every": "1d", "days": ["Mon", "FRI"], "command": ["true"]}
    )
    assert definition.days == (Weekday.MON, Weekday.FRI)
