"""Unit tests for when mngr's published state lets the chat type into grok."""

from pathlib import Path

import pytest

from imbue.chat.activity_state import ACTIVE_MARKER_FILENAME
from imbue.chat.harnesses.grok.turn_gate import is_ready_to_type


@pytest.mark.parametrize(
    ("lifecycle_state", "is_marker_present", "expected"),
    [
        pytest.param("WAITING", False, True, id="waiting"),
        pytest.param("RUNNING_BACKGROUND", False, True, id="background_work_runs"),
        pytest.param("RUNNING", False, False, id="running"),
        pytest.param("WAITING", True, False, id="the_marker_is_ahead_of_waiting"),
        pytest.param("STOPPED", False, False, id="stopped"),
        pytest.param("DONE", False, False, id="exited"),
        pytest.param("UNKNOWN", False, False, id="unobservable"),
        pytest.param(None, False, False, id="untracked"),
    ],
)
def test_the_chat_types_only_while_mngr_reports_the_agent_alive_and_idle(
    tmp_path: Path, lifecycle_state: str | None, is_marker_present: bool, expected: bool
) -> None:
    if is_marker_present:
        (tmp_path / ACTIVE_MARKER_FILENAME).touch()

    assert is_ready_to_type(lifecycle_state, tmp_path) is expected
