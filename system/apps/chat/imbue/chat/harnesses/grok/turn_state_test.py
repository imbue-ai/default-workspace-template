"""Tests for grok's turn readings, on mngr_grok's root-turn files."""

from pathlib import Path

from imbue.chat.harnesses.grok.testing import remove_root_turn
from imbue.chat.harnesses.grok.testing import write_root_turn
from imbue.chat.harnesses.grok.turn_state import get_cancellable_turns
from imbue.chat.harnesses.grok.turn_state import is_hold_required

_SESSION = "01a156d6-f927-7091-91a4-f6692a5800da"


def test_an_agent_with_no_turn_file_holds_nothing(tmp_path: Path) -> None:
    assert is_hold_required(tmp_path) is False
    assert get_cancellable_turns(tmp_path) == frozenset()


def test_a_top_level_turn_holds_messages_and_can_be_cancelled(tmp_path: Path) -> None:
    write_root_turn(tmp_path, _SESSION, "prompt-1")

    assert is_hold_required(tmp_path) is True
    assert [(turn.session_id, turn.prompt_id) for turn in get_cancellable_turns(tmp_path)] == [(_SESSION, "prompt-1")]

    remove_root_turn(tmp_path, _SESSION)
    assert is_hold_required(tmp_path) is False


def test_a_turn_grok_starts_after_another_in_the_same_session_is_a_different_turn(tmp_path: Path) -> None:
    """A finished background shell's follow-up turn writes over the session's file with its own prompt id."""
    write_root_turn(tmp_path, _SESSION, "prompt-1")
    cancelled = get_cancellable_turns(tmp_path)

    write_root_turn(tmp_path, _SESSION, "task-completed-shell-1")

    assert cancelled.isdisjoint(get_cancellable_turns(tmp_path))
    assert is_hold_required(tmp_path) is True


def test_a_pending_wake_placeholder_holds_messages_but_is_no_turn_to_cancel(tmp_path: Path) -> None:
    """A finished background subagent's follow-up turn is about to start; a Ctrl+C now would only arm quit."""
    write_root_turn(tmp_path, _SESSION, "pending-wake:01a156db-10ae-70c2-9a0f-a0dff6d5f376")

    assert is_hold_required(tmp_path) is True
    assert get_cancellable_turns(tmp_path) == frozenset()
