import hashlib
import json
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest
from workspace_profile.snapshot import (
    SNAPSHOT_RELATIVE_PATH,
    write_workspace_profile_snapshot,
)
from workspace_profile.timezone import USER_TIMEZONE_RELATIVE_PATH, read_user_timezone
from workspace_update_control.settings import (
    UpdateSettings,
    read_update_settings,
    take_update_settings,
)

from workspace_initial_data.apply import INITIAL_DATA_FILE_NAME, apply_initial_data
from workspace_initial_data.status import (
    NO_INITIAL_DATA,
    STATUS_RELATIVE_PATH,
    InitialDataRecord,
    InitialDataState,
    PartProgress,
    applied_status,
    failed_status,
    read_initial_data_record,
    read_initial_data_status,
    write_initial_data_record,
)
from workspace_initial_data.testing import (
    BACKUPS_PART_DOCUMENT,
    PROFILE_PART_DOCUMENT,
    STORAGE_TOKEN,
    UPDATE_SETTINGS_PART_DOCUMENT,
    RecordingBackupDestination,
    initial_data_document,
    write_initial_data_document,
)

_NOW = 1_795_000_000.0
_APPLIED = applied_status(_NOW)
_STAGED_SETTINGS = UpdateSettings(
    channel="stable",
    feed_base_url="https://updates.example",
    is_auto_update_enabled=True,
    is_auto_update_enabled_here=None,
    window_start_hour=2,
    window_end_hour=5,
)


def _stored_profile(workspace_root: Path) -> object:
    return json.loads((workspace_root / SNAPSHOT_RELATIVE_PATH).read_text())


def _is_nothing_written(workspace_root: Path) -> bool:
    return not (workspace_root / "data").exists()


def test_a_document_is_applied_once_each_part_through_its_writer_and_then_deleted(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    document_path = write_initial_data_document(host_dir, initial_data_document())

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert status == _APPLIED
    assert read_initial_data_status(workspace_root) == _APPLIED
    assert _stored_profile(workspace_root) == PROFILE_PART_DOCUMENT
    assert read_user_timezone(workspace_root) == "Europe/Berlin"
    assert read_update_settings() == _STAGED_SETTINGS
    assert backup_destination.requests == [(workspace_root, BACKUPS_PART_DOCUMENT)]
    assert not document_path.exists()


def test_a_later_start_with_no_document_applies_nothing_and_keeps_the_status(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    write_initial_data_document(host_dir, initial_data_document())
    apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)
    # What the owner changed since is theirs: a second start has no document to write over it with.
    (workspace_root / USER_TIMEZONE_RELATIVE_PATH).write_text("Asia/Tokyo\n")
    recorded = (workspace_root / STATUS_RELATIVE_PATH).stat()

    status = apply_initial_data(
        host_dir, workspace_root, backup_destination, _NOW + 86_400
    )

    assert status == _APPLIED
    assert (workspace_root / STATUS_RELATIVE_PATH).stat().st_ino == recorded.st_ino
    assert read_user_timezone(workspace_root) == "Asia/Tokyo"
    assert len(backup_destination.requests) == 1


def test_a_workspace_that_was_handed_nothing_reads_none_and_records_nothing(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert status == NO_INITIAL_DATA

    assert _is_nothing_written(workspace_root)
    assert backup_destination.requests == []


@pytest.mark.parametrize("left_out", [{}, {"profile": None, "backups": None}])
def test_a_part_left_out_or_null_writes_nothing(
    workspace_root: Path,
    host_dir: Path,
    backup_destination: RecordingBackupDestination,
    left_out: dict[str, None],
) -> None:
    write_initial_data_document(
        host_dir,
        {
            "schema": 1,
            "timezone": {"zone": "Europe/Berlin"},
            "update_settings": UPDATE_SETTINGS_PART_DOCUMENT,
            **left_out,
        },
    )

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert status == _APPLIED
    assert not (workspace_root / SNAPSHOT_RELATIVE_PATH).exists()
    assert backup_destination.requests == []
    assert read_user_timezone(workspace_root) == "Europe/Berlin"
    assert read_update_settings() == _STAGED_SETTINGS


def test_a_document_with_no_parts_is_applied_and_deleted(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    document_path = write_initial_data_document(host_dir, {"schema": 1})

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert status == _APPLIED
    assert not document_path.exists()


def test_the_timezone_is_recorded_only_where_none_is(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    (workspace_root / USER_TIMEZONE_RELATIVE_PATH).parent.mkdir(parents=True)
    (workspace_root / USER_TIMEZONE_RELATIVE_PATH).write_text("Asia/Tokyo\n")
    write_initial_data_document(host_dir, initial_data_document())

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert status == _APPLIED
    assert read_user_timezone(workspace_root) == "Asia/Tokyo"


@pytest.mark.parametrize("unknown_zone", ["Nowhere/Land", ""])
def test_a_zone_the_workspace_does_not_know_is_applied_with_no_zone_written(
    workspace_root: Path,
    host_dir: Path,
    backup_destination: RecordingBackupDestination,
    unknown_zone: str,
) -> None:
    document_path = write_initial_data_document(
        host_dir, initial_data_document(timezone={"zone": unknown_zone})
    )

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert status == _APPLIED
    assert read_initial_data_status(workspace_root) == _APPLIED
    assert not document_path.exists()
    assert not (workspace_root / USER_TIMEZONE_RELATIVE_PATH).exists()
    # The other parts of the same document are written.
    assert _stored_profile(workspace_root) == PROFILE_PART_DOCUMENT
    assert read_update_settings() == _STAGED_SETTINGS
    assert backup_destination.requests == [(workspace_root, BACKUPS_PART_DOCUMENT)]
    assert read_initial_data_record(workspace_root).progress_by_part == {
        "profile": PartProgress.WRITTEN,
        "timezone": PartProgress.WRITTEN,
        "update_settings": PartProgress.WRITTEN,
        "backups": PartProgress.WRITTEN,
    }


def test_a_zone_the_workspace_does_not_know_leaves_a_recorded_zone_alone(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    (workspace_root / USER_TIMEZONE_RELATIVE_PATH).parent.mkdir(parents=True)
    (workspace_root / USER_TIMEZONE_RELATIVE_PATH).write_text("Asia/Tokyo\n")
    write_initial_data_document(
        host_dir,
        initial_data_document(timezone={"zone": "Nowhere/Land", "is_replacing": True}),
    )

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert status == _APPLIED
    assert read_user_timezone(workspace_root) == "Asia/Tokyo"


@pytest.mark.parametrize(
    "malformed_part",
    [
        {},
        {"zone": None},
        {"zone": 7},
        {"zone": ["Europe/Berlin"]},
        {"zone": "Europe/Berlin", "is_replacing": "yes"},
    ],
)
def test_a_timezone_part_of_the_wrong_shape_is_refused(
    workspace_root: Path,
    host_dir: Path,
    backup_destination: RecordingBackupDestination,
    malformed_part: dict[str, object],
) -> None:
    document_path = write_initial_data_document(
        host_dir, initial_data_document(timezone=malformed_part)
    )

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert status.state is InitialDataState.FAILED
    assert status.detail.startswith("timezone: ")
    assert "; " not in status.detail
    assert document_path.exists()
    assert not (workspace_root / USER_TIMEZONE_RELATIVE_PATH).exists()


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('{"schema": 1, "timezone": ', "not JSON"),
        ("[]", "not a JSON object"),
        ('{"timezone": {"zone": "Europe/Berlin"}}', "names no schema"),
        ('{"schema": "1"}', "names no schema"),
        ('{"schema": true}', "names no schema"),
        ('{"schema": 0}', "names no schema"),
        # A schema this template does not know may carry parts in a shape it would misread.
        (
            '{"schema": 2, "timezone": {"zone": "Europe/Berlin"}}',
            "schema is 2, and this workspace's template applies up to 1",
        ),
    ],
)
def test_a_document_this_template_cannot_apply_stays_and_reads_failed_with_nothing_written(
    workspace_root: Path,
    host_dir: Path,
    backup_destination: RecordingBackupDestination,
    text: str,
    reason: str,
) -> None:
    document_path = host_dir / INITIAL_DATA_FILE_NAME
    document_path.write_text(text)
    document_path.chmod(0o644)

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert status.state is InitialDataState.FAILED
    assert reason in status.detail
    assert status.applied_at is None
    assert read_initial_data_status(workspace_root) == status
    assert document_path.read_text() == text
    # It may hold storage credentials, so a kept one is readable by its owner alone.
    assert stat.S_IMODE(document_path.stat().st_mode) == 0o600
    assert not (workspace_root / USER_TIMEZONE_RELATIVE_PATH).exists()
    assert backup_destination.requests == []


def test_a_part_that_is_not_an_object_is_refused_and_the_others_are_written(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    document_path = write_initial_data_document(
        host_dir, initial_data_document(backups="IMBUE_CLOUD")
    )

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert status == failed_status("backups: this part is not a JSON object")
    assert read_initial_data_status(workspace_root) == status
    assert document_path.exists()
    assert backup_destination.requests == []
    assert _stored_profile(workspace_root) == PROFILE_PART_DOCUMENT
    assert read_user_timezone(workspace_root) == "Europe/Berlin"
    assert read_update_settings() == _STAGED_SETTINGS
    assert read_initial_data_record(workspace_root).progress_by_part == {
        "profile": PartProgress.WRITTEN,
        "timezone": PartProgress.WRITTEN,
        "update_settings": PartProgress.WRITTEN,
    }


def test_only_the_part_that_was_refused_is_tried_again_and_what_was_written_is_left_as_it_now_is(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    document_path = write_initial_data_document(host_dir, initial_data_document())
    backup_destination.refusal = "The storage could not be reached."

    failed = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert failed == failed_status("backups: The storage could not be reached.")
    assert document_path.exists()
    # The parts that could be written are, so the workspace has its profile and its settings all the same.
    assert _stored_profile(workspace_root) == PROFILE_PART_DOCUMENT
    assert read_update_settings() == _STAGED_SETTINGS

    # What the owner changes while the services run, through the writers the routes call.
    take_update_settings({**UPDATE_SETTINGS_PART_DOCUMENT, "channel": "beta"})
    changed_profile = {**PROFILE_PART_DOCUMENT, "written_by": "desktop"}
    write_workspace_profile_snapshot(workspace_root, changed_profile)
    backup_destination.refusal = ""
    applied = apply_initial_data(
        host_dir, workspace_root, backup_destination, _NOW + 60
    )

    assert applied == applied_status(_NOW + 60)
    assert read_initial_data_status(workspace_root) == applied
    assert not document_path.exists()
    assert backup_destination.requests == [
        (workspace_root, BACKUPS_PART_DOCUMENT),
        (workspace_root, BACKUPS_PART_DOCUMENT),
    ]
    stored_settings = read_update_settings()
    assert stored_settings is not None and stored_settings.channel == "beta"
    assert _stored_profile(workspace_root) == changed_profile


def test_a_part_that_stays_refused_is_the_only_one_tried_however_often_the_services_start(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    document_path = write_initial_data_document(
        host_dir, initial_data_document(timezone={"zone": 7})
    )

    first = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)
    take_update_settings({**UPDATE_SETTINGS_PART_DOCUMENT, "channel": "beta"})
    second = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW + 60)
    third = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW + 120)

    assert first.state is InitialDataState.FAILED
    assert first.detail.startswith("timezone: ")
    assert "; " not in first.detail
    assert first == second == third
    assert document_path.exists()
    assert len(backup_destination.requests) == 1
    stored_settings = read_update_settings()
    assert stored_settings is not None and stored_settings.channel == "beta"
    assert read_initial_data_record(workspace_root).progress_by_part == {
        "profile": PartProgress.WRITTEN,
        "update_settings": PartProgress.WRITTEN,
        "backups": PartProgress.WRITTEN,
    }


def test_a_record_that_cannot_be_read_keeps_every_part_of_a_kept_document_from_being_written(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    document_path = write_initial_data_document(host_dir, initial_data_document())
    backup_destination.refusal = "The storage could not be reached."
    apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)
    backup_destination.refusal = ""
    changed_profile = {**PROFILE_PART_DOCUMENT, "written_by": "desktop"}
    write_workspace_profile_snapshot(workspace_root, changed_profile)
    # With no zone recorded, a second write of the timezone part would show.
    (workspace_root / USER_TIMEZONE_RELATIVE_PATH).unlink()
    record_path = workspace_root / STATUS_RELATIVE_PATH
    record_path.write_text("not json")
    document_path.chmod(0o644)

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW + 60)

    assert status == failed_status(
        "The record of what was applied cannot be read, so no part of the document is written."
    )
    assert len(backup_destination.requests) == 1
    assert _stored_profile(workspace_root) == changed_profile
    assert not (workspace_root / USER_TIMEZONE_RELATIVE_PATH).exists()
    assert document_path.exists()
    assert stat.S_IMODE(document_path.stat().st_mode) == 0o600
    assert record_path.read_text() == "not json"


def test_a_record_that_cannot_be_read_reads_none_while_there_is_no_document(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    record_path = workspace_root / STATUS_RELATIVE_PATH
    record_path.parent.mkdir(parents=True)
    record_path.write_text("not json")

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert status == NO_INITIAL_DATA
    assert record_path.read_text() == "not json"
    assert backup_destination.requests == []


def test_a_document_that_cannot_be_read_stays_and_reads_failed_with_nothing_written(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    document_path = host_dir / INITIAL_DATA_FILE_NAME
    document_path.mkdir()

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert status.state is InitialDataState.FAILED
    assert status.detail.startswith("The document could not be read: ")
    assert read_initial_data_status(workspace_root) == status
    assert document_path.is_dir()
    assert not (workspace_root / USER_TIMEZONE_RELATIVE_PATH).exists()
    assert backup_destination.requests == []


def test_a_document_that_cannot_be_deleted_reads_failed_and_the_next_start_deletes_it_and_writes_nothing(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    document = initial_data_document()
    document_path = write_initial_data_document(host_dir, document)

    def _take_the_destination_and_put_a_directory_in_the_documents_place(
        root: Path, request: Mapping[str, object]
    ) -> None:
        backup_destination(root, request)
        document_path.unlink()
        document_path.mkdir()

    failed = apply_initial_data(
        host_dir,
        workspace_root,
        _take_the_destination_and_put_a_directory_in_the_documents_place,
        _NOW,
    )

    assert failed.state is InitialDataState.FAILED
    assert failed.detail.startswith(
        "Every part was written, and the document could not be deleted: "
    )
    assert read_initial_data_status(workspace_root) == failed
    assert read_initial_data_record(workspace_root).progress_by_part == {
        "profile": PartProgress.WRITTEN,
        "timezone": PartProgress.WRITTEN,
        "update_settings": PartProgress.WRITTEN,
        "backups": PartProgress.WRITTEN,
    }

    # The document is there again at the next start, after the owner changed what it had set.
    document_path.rmdir()
    write_initial_data_document(host_dir, document)
    take_update_settings({**UPDATE_SETTINGS_PART_DOCUMENT, "channel": "beta"})
    applied = apply_initial_data(
        host_dir, workspace_root, backup_destination, _NOW + 60
    )

    assert applied == applied_status(_NOW + 60)
    assert read_initial_data_status(workspace_root) == applied
    assert not document_path.exists()
    assert len(backup_destination.requests) == 1
    stored_settings = read_update_settings()
    assert stored_settings is not None and stored_settings.channel == "beta"


def test_a_document_that_is_gone_when_the_step_comes_to_delete_it_is_applied(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    document_path = write_initial_data_document(host_dir, initial_data_document())

    def _take_the_destination_and_remove_the_document(
        root: Path, request: Mapping[str, object]
    ) -> None:
        backup_destination(root, request)
        document_path.unlink()

    status = apply_initial_data(
        host_dir, workspace_root, _take_the_destination_and_remove_the_document, _NOW
    )

    assert status == _APPLIED
    assert read_initial_data_status(workspace_root) == _APPLIED
    assert len(backup_destination.requests) == 1


def _put_a_link_to_nowhere_in_the_records_place(
    workspace_root: Path, tmp_path: Path
) -> Path:
    """Leave the record's directory as something that reads as no record and takes no file; answers the link."""
    record_directory = (workspace_root / STATUS_RELATIVE_PATH).parent
    record_directory.parent.mkdir(parents=True)
    record_directory.symlink_to(tmp_path / "nowhere")
    return record_directory


def test_a_part_that_cannot_be_recorded_as_begun_is_not_written_and_is_tried_at_the_next_start(
    workspace_root: Path,
    host_dir: Path,
    tmp_path: Path,
    backup_destination: RecordingBackupDestination,
) -> None:
    document_path = write_initial_data_document(host_dir, initial_data_document())
    record_directory = _put_a_link_to_nowhere_in_the_records_place(
        workspace_root, tmp_path
    )

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert status == failed_status(
        "; ".join(
            f"{name}: it could not be recorded that this part is about to be written"
            for name in ("profile", "timezone", "update_settings", "backups")
        )
    )
    assert document_path.exists()
    assert not (workspace_root / SNAPSHOT_RELATIVE_PATH).exists()
    assert not (workspace_root / USER_TIMEZONE_RELATIVE_PATH).exists()
    assert read_update_settings() is None
    assert backup_destination.requests == []

    record_directory.unlink()
    applied = apply_initial_data(
        host_dir, workspace_root, backup_destination, _NOW + 60
    )

    assert applied == applied_status(_NOW + 60)
    assert not document_path.exists()
    assert read_user_timezone(workspace_root) == "Europe/Berlin"
    assert backup_destination.requests == [(workspace_root, BACKUPS_PART_DOCUMENT)]


def test_a_document_whose_outcome_cannot_be_recorded_stays_and_reads_failed(
    workspace_root: Path,
    host_dir: Path,
    tmp_path: Path,
    backup_destination: RecordingBackupDestination,
) -> None:
    document_path = write_initial_data_document(host_dir, {"schema": 1})
    document_path.chmod(0o644)
    record_directory = _put_a_link_to_nowhere_in_the_records_place(
        workspace_root, tmp_path
    )

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert status == failed_status(
        "Every part was written, and that could not be recorded."
    )
    assert stat.S_IMODE(document_path.stat().st_mode) == 0o600

    record_directory.unlink()
    applied = apply_initial_data(
        host_dir, workspace_root, backup_destination, _NOW + 60
    )

    assert applied == applied_status(_NOW + 60)
    assert read_initial_data_status(workspace_root) == applied
    assert not document_path.exists()


class _StepKilled(BaseException):
    """Stands in for the step being killed in the middle of a part."""


def test_a_part_the_step_was_killed_after_writing_and_before_recording_is_never_written_again(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    document_path = write_initial_data_document(host_dir, initial_data_document())

    def _take_the_destination_and_die(
        root: Path, request: Mapping[str, object]
    ) -> None:
        backup_destination(root, request)
        raise _StepKilled()

    with pytest.raises(_StepKilled):
        apply_initial_data(
            host_dir, workspace_root, _take_the_destination_and_die, _NOW
        )

    # The record already says so, were the step never to run again.
    interrupted = failed_status(
        "backups: this part was being written when the step was interrupted, so it is not written again"
    )
    assert read_initial_data_status(workspace_root) == interrupted
    assert document_path.exists()

    at_the_next_start = apply_initial_data(
        host_dir, workspace_root, backup_destination, _NOW + 60
    )

    assert at_the_next_start == interrupted
    assert read_initial_data_status(workspace_root) == interrupted
    assert len(backup_destination.requests) == 1
    # Nothing is left to try, so the document and the credentials in it go.
    assert not document_path.exists()
    assert (
        apply_initial_data(host_dir, workspace_root, backup_destination, _NOW + 120)
        == interrupted
    )


def test_a_part_the_step_was_killed_in_does_not_keep_the_refused_parts_from_being_tried(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    document_path = write_initial_data_document(
        host_dir, initial_data_document(timezone={"zone": 7})
    )

    def _take_the_destination_and_die(
        root: Path, request: Mapping[str, object]
    ) -> None:
        backup_destination(root, request)
        raise _StepKilled()

    with pytest.raises(_StepKilled):
        apply_initial_data(
            host_dir, workspace_root, _take_the_destination_and_die, _NOW
        )
    interrupted = "backups: this part was being written when the step was interrupted, so it is not written again"
    assert read_initial_data_status(workspace_root) == failed_status(
        f"timezone: zone is an IANA timezone name, and is_replacing true or false; {interrupted}"
    )

    at_the_next_start = apply_initial_data(
        host_dir, workspace_root, backup_destination, _NOW + 60
    )

    # The timezone's refusal is its writer's of this start, and the document stays for the start after it.
    assert at_the_next_start == failed_status(
        f"{interrupted}; timezone: zone is an IANA timezone name, and is_replacing true or false"
    )
    assert len(backup_destination.requests) == 1
    assert document_path.exists()
    assert read_initial_data_record(workspace_root).progress_by_part == {
        "profile": PartProgress.WRITTEN,
        "update_settings": PartProgress.WRITTEN,
        "backups": PartProgress.WRITING,
    }


def test_a_part_the_step_was_killed_in_does_not_keep_the_parts_not_begun_from_being_written(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    document_path = write_initial_data_document(host_dir, initial_data_document())
    record = InitialDataRecord(
        status=failed_status("update_settings: interrupted"),
        document_digest=hashlib.sha256(document_path.read_bytes()).hexdigest(),
        progress_by_part={
            "profile": PartProgress.WRITTEN,
            "timezone": PartProgress.WRITTEN,
            "update_settings": PartProgress.WRITING,
        },
    )
    write_initial_data_record(workspace_root, record)

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert status.state is InitialDataState.FAILED
    assert status.detail.startswith("update_settings: this part was being written")
    assert read_update_settings() is None
    assert not (workspace_root / SNAPSHOT_RELATIVE_PATH).exists()
    assert backup_destination.requests == [(workspace_root, BACKUPS_PART_DOCUMENT)]
    assert not document_path.exists()


def test_a_document_left_again_with_the_bytes_of_one_already_applied_is_deleted_and_writes_nothing(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    write_initial_data_document(host_dir, initial_data_document())
    apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)
    take_update_settings({**UPDATE_SETTINGS_PART_DOCUMENT, "channel": "beta"})
    left_again = write_initial_data_document(host_dir, initial_data_document())

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW + 60)

    assert status == _APPLIED
    assert not left_again.exists()
    assert len(backup_destination.requests) == 1
    stored_settings = read_update_settings()
    assert stored_settings is not None and stored_settings.channel == "beta"


def test_every_part_a_writer_refuses_is_named_and_no_value_of_the_document_is_quoted(
    workspace_root: Path,
    host_dir: Path,
    backup_destination: RecordingBackupDestination,
    logged: list[str],
) -> None:
    write_initial_data_document(
        host_dir,
        initial_data_document(
            profile={"snapshot_format": "1", "account_id": "value-of-the-profile"},
            timezone={"zone": 7, "note": "value-of-the-timezone"},
            update_settings={
                **UPDATE_SETTINGS_PART_DOCUMENT,
                "channel": "../value-of-the-settings",
            },
        ),
    )

    status = apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    assert status.state is InitialDataState.FAILED
    assert [refusal.split(":")[0] for refusal in status.detail.split("; ")] == [
        "profile",
        "timezone",
        "update_settings",
    ]
    assert any(status.detail in message for message in logged)
    said = [status.detail, (workspace_root / STATUS_RELATIVE_PATH).read_text(), *logged]
    for value in (
        STORAGE_TOKEN,
        "value-of-the-profile",
        "value-of-the-timezone",
        "value-of-the-settings",
    ):
        assert not any(value in text for text in said)
    # The backup part was sound, and reached its writer.
    assert len(backup_destination.requests) == 1


def test_the_update_settings_keep_what_the_workspace_holds_of_its_own(
    workspace_root: Path, host_dir: Path, backup_destination: RecordingBackupDestination
) -> None:
    write_initial_data_document(
        host_dir,
        {
            "schema": 1,
            "update_settings": {
                **UPDATE_SETTINGS_PART_DOCUMENT,
                "window_start_hour_here": 22,
                "window_end_hour_here": 23,
            },
        },
    )
    apply_initial_data(host_dir, workspace_root, backup_destination, _NOW)

    # A second document, as another create of the same machine would leave, names none of the workspace's own.
    write_initial_data_document(
        host_dir,
        {
            "schema": 1,
            "update_settings": {**UPDATE_SETTINGS_PART_DOCUMENT, "channel": "beta"},
        },
    )
    apply_initial_data(host_dir, workspace_root, backup_destination, _NOW + 60)

    stored = read_update_settings()
    assert stored is not None
    assert (stored.channel, stored.window_hours) == ("beta", (22, 23))


def test_the_update_settings_are_refused_from_anywhere_but_the_workspaces_root(
    workspace_root: Path,
    host_dir: Path,
    tmp_path: Path,
    backup_destination: RecordingBackupDestination,
) -> None:
    write_initial_data_document(
        host_dir, {"schema": 1, "update_settings": UPDATE_SETTINGS_PART_DOCUMENT}
    )
    elsewhere = tmp_path / "another-workspace"
    elsewhere.mkdir()

    status = apply_initial_data(host_dir, elsewhere, backup_destination, _NOW)

    assert status.state is InitialDataState.FAILED
    assert "not the working directory" in status.detail
    assert _is_nothing_written(workspace_root)
