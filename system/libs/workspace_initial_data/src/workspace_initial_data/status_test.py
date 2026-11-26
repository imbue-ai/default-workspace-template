from pathlib import Path

import pytest

from workspace_initial_data.status import (
    NO_INITIAL_DATA,
    NO_RECORD,
    STATUS_RELATIVE_PATH,
    InitialDataRecord,
    InitialDataStatus,
    PartProgress,
    applied_status,
    failed_status,
    read_initial_data_record,
    read_initial_data_status,
    write_initial_data_record,
)


@pytest.mark.parametrize(
    "status",
    [
        applied_status(1_795_000_000.5),
        failed_status(
            "timezone: zone is an IANA timezone name, and is_replacing true or false"
        ),
    ],
)
def test_a_status_is_read_back_as_it_was_recorded(
    workspace_root: Path, status: InitialDataStatus
) -> None:
    write_initial_data_record(workspace_root, InitialDataRecord(status=status))

    assert read_initial_data_status(workspace_root) == status


def test_a_record_is_read_back_with_how_far_each_part_of_its_document_has_come(
    workspace_root: Path,
) -> None:
    record = InitialDataRecord(
        status=failed_status(
            "timezone: zone is an IANA timezone name, and is_replacing true or false"
        ),
        document_digest="5f" * 32,
        progress_by_part={
            "profile": PartProgress.WRITTEN,
            "backups": PartProgress.WRITING,
        },
    )

    write_initial_data_record(workspace_root, record)

    assert read_initial_data_record(workspace_root) == record
    assert not list((workspace_root / STATUS_RELATIVE_PATH).parent.glob(".*"))


def test_a_workspace_with_no_record_was_handed_nothing(workspace_root: Path) -> None:
    assert read_initial_data_record(workspace_root) == NO_RECORD
    assert NO_RECORD.status == NO_INITIAL_DATA


def test_a_record_whose_parts_are_no_progress_reads_as_none(
    workspace_root: Path,
) -> None:
    path = workspace_root / STATUS_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"schema": 1, "state": "FAILED", "detail": "x", "applied_at": null, "parts": {"profile": "HALF"}}'
    )

    assert read_initial_data_record(workspace_root) == NO_RECORD


@pytest.mark.parametrize(
    "stored", ["", "not json", "[]", '{"state": "HALF"}', '{"detail": "no state"}']
)
def test_a_record_that_is_no_status_reads_none(
    workspace_root: Path, stored: str
) -> None:
    path = workspace_root / STATUS_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text(stored)

    assert read_initial_data_status(workspace_root) == NO_INITIAL_DATA


def test_a_member_a_later_template_recorded_is_ignored(workspace_root: Path) -> None:
    path = workspace_root / STATUS_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"schema": 2, "state": "APPLIED", "detail": "", "applied_at": 5.0, "sources": ["timezone"]}'
    )

    assert read_initial_data_status(workspace_root) == applied_status(5.0)
