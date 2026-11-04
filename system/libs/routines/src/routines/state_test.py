import json

import pytest

from imbue.imbue_common.model_update import to_update
from routines.data_types import RoutineState
from routines.errors import StateRemovalError
from routines.primitives import RunId
from routines.state import FileStateStore
from routines.testing import TEST_ROUTINE_ID


def test_a_routine_with_no_state_file_starts_fresh(states: FileStateStore) -> None:
    assert states.read(TEST_ROUTINE_ID) == RoutineState()


def test_an_update_is_written_whole_and_read_back(states: FileStateStore) -> None:
    updated = states.update(
        TEST_ROUTINE_ID, lambda state: state.model_copy_update(to_update(state.field_ref().is_enabled, False))
    )
    assert updated.is_enabled is False
    assert states.read(TEST_ROUTINE_ID).is_enabled is False
    assert states.paths.state_path(TEST_ROUTINE_ID).is_file()


def test_a_state_file_that_does_not_parse_reads_fresh_and_is_set_aside_by_the_next_write(
    states: FileStateStore,
) -> None:
    path = states.paths.state_path(TEST_ROUTINE_ID)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert states.read(TEST_ROUTINE_ID) == RoutineState()
    assert path.read_text() == "{not json", "a read never rewrites the file"

    updated = states.update(
        TEST_ROUTINE_ID, lambda state: state.model_copy_update(to_update(state.field_ref().is_enabled, False))
    )

    assert updated.is_enabled is False
    assert states.read(TEST_ROUTINE_ID).is_enabled is False
    assert path.with_suffix(".json.bad").read_text() == "{not json"


def test_removing_a_routine_drops_its_whole_state_directory(states: FileStateStore) -> None:
    states.update(TEST_ROUTINE_ID, lambda state: state)
    states.remove(TEST_ROUTINE_ID)
    assert not states.paths.routine_state_dir(TEST_ROUTINE_ID).exists()


def test_a_state_directory_that_cannot_be_deleted_whole_is_an_error_not_a_warning(states: FileStateStore) -> None:
    """A removal that leaves state behind would silently apply it to a routine re-added under the same id. The
    state directory is a symlink here, which a whole-tree delete refuses whoever runs it (root included)."""
    elsewhere = states.paths.state_dir / "elsewhere"
    (elsewhere / "runs" / "run-1").mkdir(parents=True)
    (elsewhere / "runs" / "run-1" / "log").write_text("x")
    states.paths.routine_state_dir(TEST_ROUTINE_ID).symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(StateRemovalError):
        states.remove(TEST_ROUTINE_ID)

    assert (elsewhere / "runs" / "run-1" / "log").is_file()


def test_a_field_a_newer_writer_added_is_ignored_in_the_state_and_in_its_runs(states: FileStateStore) -> None:
    """The app may read a state file written by a newer library mid-update; one unknown field must not reset it."""
    path = states.paths.state_path(TEST_ROUTINE_ID)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "is_enabled": False,
                "newer_field": 1,
                "current_run": {
                    "run_id": str(RunId.generate()),
                    "pid": 1,
                    "trigger": "MANUAL",
                    "started_at": "2026-10-20T15:00:00+00:00",
                    "newer_field": 1,
                },
                "last_run": {
                    "run_id": str(RunId.generate()),
                    "trigger": "MANUAL",
                    "kind": "COMMAND",
                    "outcome": "SUCCEEDED",
                    "summary": "",
                    "started_at": "2026-10-20T14:00:00+00:00",
                    "finished_at": "2026-10-20T14:01:00+00:00",
                    "newer_field": 1,
                },
            }
        )
    )
    state = states.read(TEST_ROUTINE_ID)
    assert state.is_enabled is False
    assert state.current_run is not None and state.last_run is not None
