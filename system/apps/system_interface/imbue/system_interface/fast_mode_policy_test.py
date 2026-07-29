"""Tests for the workspace fast-mode decision and the launch-state resolver."""

import json
from pathlib import Path

from loguru import logger

from imbue.system_interface.fast_mode_policy import UNDECIDED_FAST_MODE_DECISION
from imbue.system_interface.fast_mode_policy import WorkspaceFastModeDecision
from imbue.system_interface.fast_mode_policy import get_workspace_fast_mode_decision_path
from imbue.system_interface.fast_mode_policy import read_fast_mode_setting
from imbue.system_interface.fast_mode_policy import read_workspace_fast_mode_decision
from imbue.system_interface.fast_mode_policy import resolve_launch_fast_mode
from imbue.system_interface.fast_mode_policy import write_workspace_fast_mode_decision


def test_undecided_workspace_starts_chats_fast() -> None:
    """The grace period exists precisely so an unanswered workspace runs fast."""
    assert UNDECIDED_FAST_MODE_DECISION.is_decided is False
    assert UNDECIDED_FAST_MODE_DECISION.is_fast_mode_enabled is True


def test_decision_round_trips_through_the_file(tmp_path: Path) -> None:
    decision_path = get_workspace_fast_mode_decision_path(tmp_path)
    for is_enabled in (False, True):
        write_workspace_fast_mode_decision(
            decision_path, WorkspaceFastModeDecision(is_decided=True, is_fast_mode_enabled=is_enabled)
        )
        recorded = read_workspace_fast_mode_decision(decision_path)
        assert recorded.is_decided is True
        assert recorded.is_fast_mode_enabled is is_enabled


def test_writing_a_decision_replaces_the_previous_one(tmp_path: Path) -> None:
    """A user who changes their mind must not leave two answers on disk."""
    decision_path = get_workspace_fast_mode_decision_path(tmp_path)
    write_workspace_fast_mode_decision(
        decision_path, WorkspaceFastModeDecision(is_decided=True, is_fast_mode_enabled=True)
    )
    write_workspace_fast_mode_decision(
        decision_path, WorkspaceFastModeDecision(is_decided=True, is_fast_mode_enabled=False)
    )
    assert read_workspace_fast_mode_decision(decision_path).is_fast_mode_enabled is False
    # The atomic write must not leave its temporary file behind.
    assert sorted(p.name for p in decision_path.parent.iterdir()) == [decision_path.name]


def test_absent_or_corrupt_decision_reads_as_undecided(tmp_path: Path) -> None:
    """Falling back to undecided keeps the prompt available rather than silently
    locking the workspace into a setting nobody chose."""
    assert read_workspace_fast_mode_decision(tmp_path / "missing.json") == UNDECIDED_FAST_MODE_DECISION

    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("{not valid json")
    assert read_workspace_fast_mode_decision(corrupt_path) == UNDECIDED_FAST_MODE_DECISION

    wrong_shape_path = tmp_path / "wrong.json"
    wrong_shape_path.write_text(json.dumps({"is_decided": "yes"}))
    assert read_workspace_fast_mode_decision(wrong_shape_path) == UNDECIDED_FAST_MODE_DECISION


def test_missing_fast_mode_key_is_distinguishable_from_false(tmp_path: Path) -> None:
    """Claude Code deletes the key rather than writing false, so the reader has to
    report absence as absence -- collapsing it to False would let a lower-precedence
    file silently override a higher-precedence one."""
    absent_path = tmp_path / "absent.json"
    absent_path.write_text(json.dumps({"model": "opus[1m]"}))
    assert read_fast_mode_setting(absent_path) is None

    false_path = tmp_path / "false.json"
    false_path.write_text(json.dumps({"fastMode": False}))
    assert read_fast_mode_setting(false_path) is False

    assert read_fast_mode_setting(tmp_path / "nope.json") is None


def test_unreadable_settings_read_as_unset_but_are_logged(tmp_path: Path) -> None:
    """A file that will not open reads as unset like an absent one, so the layering
    still resolves -- but unlike an absent one it says so, since it may well have
    held the value that decides the answer."""
    unreadable_path = tmp_path / "settings.json"
    unreadable_path.mkdir()

    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(message), level="WARNING")
    try:
        assert read_fast_mode_setting(unreadable_path) is None
    finally:
        logger.remove(sink_id)

    assert any(str(unreadable_path) in message for message in messages)


def test_managed_settings_outrank_user_settings(tmp_path: Path) -> None:
    """mngr passes the managed file via --settings, which Claude layers above the
    shared user settings -- so it decides what the session launched with."""
    user_path = tmp_path / "settings.json"
    managed_path = tmp_path / "managed.json"
    user_path.write_text(json.dumps({"fastMode": True}))
    managed_path.write_text(json.dumps({"fastMode": False}))
    assert resolve_launch_fast_mode(claude_settings_path=user_path, managed_settings_path=managed_path) is False

    managed_path.write_text(json.dumps({"fastMode": True}))
    user_path.write_text(json.dumps({"model": "opus[1m]"}))
    assert resolve_launch_fast_mode(claude_settings_path=user_path, managed_settings_path=managed_path) is True


def test_user_settings_decide_when_managed_leaves_fast_mode_unset(tmp_path: Path) -> None:
    user_path = tmp_path / "settings.json"
    managed_path = tmp_path / "managed.json"
    managed_path.write_text(json.dumps({"hooks": {}}))

    user_path.write_text(json.dumps({"fastMode": True}))
    assert resolve_launch_fast_mode(claude_settings_path=user_path, managed_settings_path=managed_path) is True

    # An absent key in the user layer means off: that is how /fast off records itself.
    user_path.write_text(json.dumps({"model": "opus[1m]"}))
    assert resolve_launch_fast_mode(claude_settings_path=user_path, managed_settings_path=managed_path) is False


def test_fast_mode_is_off_when_neither_settings_file_exists(tmp_path: Path) -> None:
    assert (
        resolve_launch_fast_mode(
            claude_settings_path=tmp_path / "missing.json",
            managed_settings_path=tmp_path / "also-missing.json",
        )
        is False
    )
