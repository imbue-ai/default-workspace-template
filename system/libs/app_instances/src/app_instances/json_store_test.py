import json
from datetime import timezone
from pathlib import Path

import pytest
from app_manifest.primitives import ActionId, AppName
from imbue.imbue_common.model_update import to_update

from app_instances.data_types import InstanceLifetime, InstanceStatus
from app_instances.errors import (
    InstanceConflictError,
    InstanceStoreError,
    InvalidInstanceValueError,
    InvalidParamsError,
    LocationNotTrackedError,
    NotRenameableError,
    UnknownActionError,
    UnknownInstanceError,
)
from app_instances.json_store import (
    JsonStoreInstanceSource,
    allocate_key,
    app_store_path,
    instance_number,
)
from app_instances.primitives import (
    InstanceKey,
    InstanceKeyPrefix,
    InstanceTitle,
    LocationPath,
)

_NEW = ActionId("new")
_FILES = InstanceKeyPrefix("files")


@pytest.mark.parametrize(
    ("taken", "expected"),
    [
        (set(), "files-1"),
        ({"files-1", "files-2"}, "files-3"),
        ({"files-1", "files-3"}, "files-2"),
        ({"files-2"}, "files-1"),
        ({"other-1", "files-01", "files-x", "myfiles-1"}, "files-1"),
    ],
)
def test_allocate_key_mints_the_lowest_free_number_ignoring_foreign_keys(
    taken: set[str], expected: str
) -> None:
    assert allocate_key(_FILES, taken) == expected


def test_instance_number_reads_only_keys_the_allocator_minted() -> None:
    assert instance_number(_FILES, "files-12") == 12
    assert instance_number(_FILES, "files-0") is None
    assert instance_number(_FILES, "myfiles-1") is None
    assert instance_number(_FILES, "files") is None


def test_app_store_path_is_under_the_apps_data_dir() -> None:
    assert app_store_path(AppName("files")) == Path("data/.apps/files/instances.json")


def test_create_stores_the_default_path_with_the_templated_title(
    files_store: JsonStoreInstanceSource,
) -> None:
    record = files_store.create_instance(_NEW, {})

    assert record.key == "files-1"
    assert record.url == "/"
    assert record.title == "File Viewer 1"
    assert record.status is InstanceStatus.IDLE
    assert record.lifetime is InstanceLifetime.REFERENCED
    assert record.renameable is False
    assert record.last_active is not None
    assert record.last_active.tzinfo == timezone.utc
    assert files_store.list_instances() == [record]


def test_create_stores_the_requested_path_and_numbers_instances_in_order(
    files_store: JsonStoreInstanceSource,
) -> None:
    first = files_store.create_instance(_NEW, {"path": "/data/docs/"})
    second = files_store.create_instance(_NEW, {"path": "/data/"})

    assert (first.key, first.url, first.title) == (
        "files-1",
        "/data/docs/",
        "File Viewer 1",
    )
    assert (second.key, second.url, second.title) == (
        "files-2",
        "/data/",
        "File Viewer 2",
    )


def test_create_rejects_other_actions_unknown_params_and_bad_paths(
    files_store: JsonStoreInstanceSource,
) -> None:
    with pytest.raises(UnknownActionError, match="unknown action 'open'"):
        files_store.create_instance(ActionId("open"), {})
    with pytest.raises(InvalidParamsError, match="unknown params \\['workdir'\\]"):
        files_store.create_instance(_NEW, {"workdir": "/x"})
    with pytest.raises(InvalidInstanceValueError, match="single slash"):
        files_store.create_instance(_NEW, {"path": "docs"})
    assert files_store.list_instances() == []


def test_delete_drops_the_record_and_frees_its_number(
    files_store: JsonStoreInstanceSource,
) -> None:
    files_store.create_instance(_NEW, {})
    files_store.create_instance(_NEW, {})

    files_store.delete_instance(InstanceKey("files-1"))
    reused = files_store.create_instance(_NEW, {})

    assert [record.key for record in files_store.list_instances()] == [
        "files-2",
        "files-1",
    ]
    assert reused.key == "files-1"


def test_delete_of_an_unknown_key_changes_nothing(
    files_store: JsonStoreInstanceSource,
) -> None:
    files_store.create_instance(_NEW, {})
    before = files_store.store_path.read_text()

    files_store.delete_instance(InstanceKey("files-7"))

    assert files_store.store_path.read_text() == before


def test_rename_is_refused_when_the_store_is_not_renameable(
    files_store: JsonStoreInstanceSource,
) -> None:
    files_store.create_instance(_NEW, {})

    with pytest.raises(NotRenameableError):
        files_store.rename_instance(InstanceKey("files-1"), InstanceTitle("Mine"))


def test_rename_retitles_and_refuses_unknown_keys_and_title_collisions(
    renameable_store: JsonStoreInstanceSource,
) -> None:
    renameable_store.create_instance(_NEW, {})
    renameable_store.create_instance(_NEW, {})

    renamed = renameable_store.rename_instance(
        InstanceKey("note-1"), InstanceTitle("Plans")
    )

    assert renamed.title == "Plans"
    assert renamed.renameable is True
    assert [record.title for record in renameable_store.list_instances()] == [
        "Plans",
        "Note 2",
    ]
    with pytest.raises(InstanceConflictError, match="already titled 'Plans'"):
        renameable_store.rename_instance(InstanceKey("note-2"), InstanceTitle("Plans"))
    with pytest.raises(UnknownInstanceError):
        renameable_store.rename_instance(InstanceKey("note-9"), InstanceTitle("x"))


def test_set_location_replaces_the_url_and_refreshes_last_active(
    files_store: JsonStoreInstanceSource,
) -> None:
    created = files_store.create_instance(_NEW, {"path": "/data/"})

    relocated = files_store.set_location(
        InstanceKey("files-1"), LocationPath("/data/docs/?sort=name")
    )

    assert relocated.url == "/data/docs/?sort=name"
    assert relocated.last_active is not None
    assert created.last_active is not None
    assert relocated.last_active >= created.last_active
    assert files_store.list_instances() == [relocated]
    with pytest.raises(UnknownInstanceError):
        files_store.set_location(InstanceKey("files-9"), LocationPath("/"))


def test_set_location_is_refused_when_the_store_does_not_track_it(
    renameable_store: JsonStoreInstanceSource,
) -> None:
    renameable_store.create_instance(_NEW, {})

    with pytest.raises(LocationNotTrackedError):
        renameable_store.set_location(InstanceKey("note-1"), LocationPath("/"))


def test_records_survive_a_reload_through_the_documented_file_shape(
    files_store: JsonStoreInstanceSource,
) -> None:
    created = files_store.create_instance(_NEW, {"path": "/data/docs/"})

    document = json.loads(files_store.store_path.read_text())
    reloaded = files_store.model_copy()

    assert document == {"version": 1, "instances": [created.model_dump(mode="json")]}
    assert reloaded.list_instances() == [created]


def test_a_stray_temp_file_from_a_crashed_write_is_ignored(
    files_store: JsonStoreInstanceSource,
) -> None:
    files_store.create_instance(_NEW, {})
    stray = files_store.store_path.parent / f"{files_store.store_path.name}.crashed.tmp"
    stray.write_text("{ half a document")

    files_store.create_instance(_NEW, {})

    assert [record.key for record in files_store.list_instances()] == [
        "files-1",
        "files-2",
    ]
    assert stray.read_text() == "{ half a document"


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("{ not json", "not valid JSON"),
        ('{"version": 2, "instances": []}', "is version 2"),
        ('{"version": 1, "instances": [{"key": "files-1"}]}', "malformed"),
        ('{"version": 1}', "malformed"),
    ],
)
def test_an_unreadable_store_raises_instead_of_reading_as_empty(
    files_store: JsonStoreInstanceSource, content: str, reason: str
) -> None:
    files_store.store_path.parent.mkdir(parents=True, exist_ok=True)
    files_store.store_path.write_text(content)

    with pytest.raises(InstanceStoreError, match=reason):
        files_store.list_instances()
    with pytest.raises(InstanceStoreError, match=reason):
        files_store.create_instance(_NEW, {})


def test_a_temp_file_that_cannot_be_created_is_a_store_error(
    files_store: JsonStoreInstanceSource,
) -> None:
    # The store's own name fits a filesystem's 255-byte limit; the temp file's longer name does not.
    long_named_store = files_store.model_copy_update(
        to_update(
            files_store.field_ref().store_path,
            files_store.store_path.parent / ("x" * 250),
        )
    )

    with pytest.raises(InstanceStoreError, match="cannot create a temporary file"):
        long_named_store.create_instance(_NEW, {})
    assert long_named_store.list_instances() == []
