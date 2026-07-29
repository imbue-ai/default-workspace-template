"""Tests for the workspace fast-mode decision, the settings resolver, and the write-through."""

import json
from pathlib import Path

import pytest
from loguru import logger

from imbue.system_interface.fast_mode_policy import FastModeSettingsError
from imbue.system_interface.fast_mode_policy import get_agent_fast_mode_write_path
from imbue.system_interface.fast_mode_policy import get_workspace_fast_mode_decision_path
from imbue.system_interface.fast_mode_policy import read_fast_mode_setting
from imbue.system_interface.fast_mode_policy import read_workspace_fast_mode_decision
from imbue.system_interface.fast_mode_policy import resolve_agent_fast_mode
from imbue.system_interface.fast_mode_policy import write_fast_mode_setting
from imbue.system_interface.fast_mode_policy import write_workspace_fast_mode_decision


def test_decision_round_trips_through_the_file(tmp_path: Path) -> None:
    decision_path = get_workspace_fast_mode_decision_path(tmp_path)
    for is_enabled in (False, True):
        write_workspace_fast_mode_decision(decision_path, is_enabled)
        assert read_workspace_fast_mode_decision(decision_path) is is_enabled


def test_writing_a_decision_replaces_the_previous_one(tmp_path: Path) -> None:
    """A user who changes their mind must not leave two answers on disk."""
    decision_path = get_workspace_fast_mode_decision_path(tmp_path)
    write_workspace_fast_mode_decision(decision_path, True)
    write_workspace_fast_mode_decision(decision_path, False)
    assert read_workspace_fast_mode_decision(decision_path) is False
    # The atomic write must not leave its temporary file behind.
    assert sorted(p.name for p in decision_path.parent.iterdir()) == [decision_path.name]


def test_absent_or_corrupt_decision_reads_as_undecided(tmp_path: Path) -> None:
    """Falling back to undecided keeps the prompt available rather than silently
    locking the workspace into a setting nobody chose."""
    assert read_workspace_fast_mode_decision(tmp_path / "missing.json") is None

    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("{not valid json")
    assert read_workspace_fast_mode_decision(corrupt_path) is None

    wrong_shape_path = tmp_path / "wrong.json"
    wrong_shape_path.write_text(json.dumps({"is_fast_mode_enabled": "yes"}))
    assert read_workspace_fast_mode_decision(wrong_shape_path) is None


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
    shared user settings -- so it decides what the agent runs with."""
    user_path = tmp_path / "settings.json"
    managed_path = tmp_path / "managed.json"
    user_path.write_text(json.dumps({"fastMode": True}))
    managed_path.write_text(json.dumps({"fastMode": False}))
    assert resolve_agent_fast_mode(claude_settings_path=user_path, managed_settings_path=managed_path) is False

    managed_path.write_text(json.dumps({"fastMode": True}))
    user_path.write_text(json.dumps({"model": "opus[1m]"}))
    assert resolve_agent_fast_mode(claude_settings_path=user_path, managed_settings_path=managed_path) is True


def test_user_settings_decide_when_managed_leaves_fast_mode_unset(tmp_path: Path) -> None:
    user_path = tmp_path / "settings.json"
    managed_path = tmp_path / "managed.json"
    managed_path.write_text(json.dumps({"hooks": {}}))

    user_path.write_text(json.dumps({"fastMode": True}))
    assert resolve_agent_fast_mode(claude_settings_path=user_path, managed_settings_path=managed_path) is True

    # An absent key in the user layer means off: that is how /fast off records itself.
    user_path.write_text(json.dumps({"model": "opus[1m]"}))
    assert resolve_agent_fast_mode(claude_settings_path=user_path, managed_settings_path=managed_path) is False


def test_fast_mode_is_off_when_neither_settings_file_exists(tmp_path: Path) -> None:
    assert (
        resolve_agent_fast_mode(
            claude_settings_path=tmp_path / "missing.json",
            managed_settings_path=tmp_path / "also-missing.json",
        )
        is False
    )


def test_shared_config_dir_writes_to_the_managed_overlay(tmp_path: Path) -> None:
    """In shared mode the config dir belongs to every agent on the host, so the change
    has to land in the agent's own managed --settings file instead."""
    agent_state_dir = tmp_path / "agent-state"
    shared_config_dir = tmp_path / "home" / ".claude"

    write_path = get_agent_fast_mode_write_path(shared_config_dir, agent_state_dir)

    assert write_path.is_relative_to(agent_state_dir)
    assert not write_path.is_relative_to(shared_config_dir)


def test_isolated_config_dir_writes_to_its_own_settings(tmp_path: Path) -> None:
    """With a per-agent config dir there is no managed overlay, and its settings.json
    is the file Claude Code itself writes."""
    agent_state_dir = tmp_path / "agent-state"
    isolated_config_dir = agent_state_dir / "plugin" / "claude" / "anthropic"

    write_path = get_agent_fast_mode_write_path(isolated_config_dir, agent_state_dir)

    assert write_path == isolated_config_dir / "settings.json"


def test_writing_fast_mode_preserves_the_rest_of_the_file(tmp_path: Path) -> None:
    """The file this targets is mngr's, and carries its hooks -- replacing it rather
    than patching it would stop the agent reporting itself active or idle."""
    settings_path = tmp_path / "mngr_managed_settings.json"
    settings_path.write_text(json.dumps({"hooks": {"SessionStart": ["mark-active"]}, "fastMode": True}))

    write_fast_mode_setting(settings_path, False)

    assert json.loads(settings_path.read_text()) == {
        "hooks": {"SessionStart": ["mark-active"]},
        "fastMode": False,
    }
    # The atomic write must not leave its temporary file behind.
    assert sorted(p.name for p in tmp_path.iterdir()) == [settings_path.name]


def test_writing_fast_mode_creates_the_file_and_its_parents(tmp_path: Path) -> None:
    """An agent whose settings file has not been written yet must still record a
    toggle, or the change would be lost at the next launch."""
    settings_path = tmp_path / "plugin" / "claude" / "mngr_managed_settings.json"

    write_fast_mode_setting(settings_path, True)

    assert json.loads(settings_path.read_text()) == {"fastMode": True}


def test_writing_fast_mode_refuses_to_clobber_unparseable_settings(tmp_path: Path) -> None:
    """Replacing a file we cannot parse would silently drop whatever mngr put in it,
    so this fails and lets the endpoint report that the change was not recorded."""
    settings_path = tmp_path / "mngr_managed_settings.json"
    settings_path.write_text("{not valid json")

    with pytest.raises(FastModeSettingsError):
        write_fast_mode_setting(settings_path, True)

    assert settings_path.read_text() == "{not valid json"


def test_writing_fast_mode_refuses_a_settings_file_that_is_not_an_object(tmp_path: Path) -> None:
    settings_path = tmp_path / "mngr_managed_settings.json"
    settings_path.write_text(json.dumps(["not", "an", "object"]))

    with pytest.raises(FastModeSettingsError):
        write_fast_mode_setting(settings_path, True)


def test_a_recorded_toggle_is_what_the_agent_resolves_to(tmp_path: Path) -> None:
    """The point of the write-through: what the UI set is what the next launch reads,
    so a restart cannot put the agent back on a setting the user turned off."""
    agent_state_dir = tmp_path / "agent-state"
    shared_config_dir = tmp_path / "home" / ".claude"
    shared_config_dir.mkdir(parents=True)
    claude_settings_path = shared_config_dir / "settings.json"
    # A stale true in the shared config, which every agent in shared mode reads.
    claude_settings_path.write_text(json.dumps({"fastMode": True}))
    managed_path = get_agent_fast_mode_write_path(shared_config_dir, agent_state_dir)
    # The value the agent was provisioned fast with.
    write_fast_mode_setting(managed_path, True)

    write_fast_mode_setting(managed_path, False)

    assert (
        resolve_agent_fast_mode(
            claude_settings_path=claude_settings_path,
            managed_settings_path=managed_path,
        )
        is False
    )
