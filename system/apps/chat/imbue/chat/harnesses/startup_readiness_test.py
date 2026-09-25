import os
from pathlib import Path

from imbue.chat.harnesses.startup_readiness import StartupReadyMarker
from imbue.chat.harnesses.startup_readiness import is_harness_starting_up

_PROCESS_STARTED = "harness_process_started"
_DELETED_AT_LAUNCH = StartupReadyMarker(filename="session_started", is_deleted_at_launch=True)
_KEPT_ACROSS_LAUNCHES = StartupReadyMarker(filename="harness_session_started", is_deleted_at_launch=False)


def _touch_at(path: Path, mtime: float) -> None:
    path.touch()
    os.utime(path, (mtime, mtime))


def test_a_harness_that_writes_no_ready_marker_never_reads_as_starting(tmp_path: Path) -> None:
    assert is_harness_starting_up(tmp_path, None, _PROCESS_STARTED) is False


def test_an_agent_whose_state_dir_is_not_on_this_host_never_reads_as_starting(tmp_path: Path) -> None:
    assert is_harness_starting_up(tmp_path / "absent", _DELETED_AT_LAUNCH, _PROCESS_STARTED) is False


def test_a_marker_deleted_at_launch_reads_as_starting_until_it_is_written(tmp_path: Path) -> None:
    _touch_at(tmp_path / _PROCESS_STARTED, 3_000_000.0)
    assert is_harness_starting_up(tmp_path, _DELETED_AT_LAUNCH, _PROCESS_STARTED) is True

    # Its presence alone is the answer: the launch removed any earlier one, so even a marker
    # older than the process-started one belongs to this process.
    _touch_at(tmp_path / _DELETED_AT_LAUNCH.filename, 2_000_000.0)
    assert is_harness_starting_up(tmp_path, _DELETED_AT_LAUNCH, _PROCESS_STARTED) is False


def test_a_marker_kept_across_launches_reads_as_starting_while_it_predates_the_launch(tmp_path: Path) -> None:
    _touch_at(tmp_path / _KEPT_ACROSS_LAUNCHES.filename, 1_000_000.0)
    _touch_at(tmp_path / _PROCESS_STARTED, 1_000_050.0)
    assert is_harness_starting_up(tmp_path, _KEPT_ACROSS_LAUNCHES, _PROCESS_STARTED) is True

    _touch_at(tmp_path / _KEPT_ACROSS_LAUNCHES.filename, 1_000_060.0)
    assert is_harness_starting_up(tmp_path, _KEPT_ACROSS_LAUNCHES, _PROCESS_STARTED) is False


def test_a_marker_kept_across_launches_reads_as_starting_while_absent(tmp_path: Path) -> None:
    _touch_at(tmp_path / _PROCESS_STARTED, 1_000_050.0)
    assert is_harness_starting_up(tmp_path, _KEPT_ACROSS_LAUNCHES, _PROCESS_STARTED) is True


def test_a_marker_with_no_process_started_marker_to_compare_against_reads_as_ready(tmp_path: Path) -> None:
    _touch_at(tmp_path / _KEPT_ACROSS_LAUNCHES.filename, 1_000_000.0)
    assert is_harness_starting_up(tmp_path, _KEPT_ACROSS_LAUNCHES, _PROCESS_STARTED) is False
