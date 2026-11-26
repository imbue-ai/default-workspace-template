from pathlib import Path

import pytest

from workspace_initial_data.status import (
    NO_INITIAL_DATA,
    STATUS_RELATIVE_PATH,
    InitialDataStatus,
    applied_status,
    failed_status,
    read_initial_data_status,
    write_initial_data_status,
)


@pytest.mark.parametrize(
    "status",
    [
        applied_status(1_795_000_000.5),
        failed_status("timezone: 'Mars' is not a timezone"),
    ],
)
def test_a_status_is_read_back_as_it_was_recorded(
    workspace_root: Path, status: InitialDataStatus
) -> None:
    write_initial_data_status(workspace_root, status)

    assert read_initial_data_status(workspace_root) == status


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
        '{"schema": 2, "state": "APPLIED", "detail": "", "applied_at": 5.0, "parts": ["timezone"]}'
    )

    assert read_initial_data_status(workspace_root) == applied_status(5.0)
