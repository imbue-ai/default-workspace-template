from pathlib import Path

import pytest

from workspace_backup_setup.state import (
    SETUP_STATE_RELATIVE_PATH,
    SetupRecord,
    read_setup_record,
)


@pytest.mark.parametrize("text", ["{ not json", '{"destination": "SOMEWHERE_ELSE"}'])
def test_a_record_that_cannot_be_read_reads_as_the_record_of_a_workspace_nothing_was_asked_of(
    workspace_root: Path, text: str
) -> None:
    record_path = workspace_root / SETUP_STATE_RELATIVE_PATH
    record_path.parent.mkdir(parents=True)
    record_path.write_text(text)

    assert read_setup_record(workspace_root) == SetupRecord()
