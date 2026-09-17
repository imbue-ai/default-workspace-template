from pathlib import Path

from imbue.chat.chat_fast_mode import ChatFastModeState
from imbue.chat.chat_fast_mode import FAST_MODE_FILENAME
from imbue.chat.chat_fast_mode import read_fast_mode_state
from imbue.chat.chat_fast_mode import write_fast_mode_state
from imbue.chat.chat_settings import FastModeMode


def test_only_on_and_an_unswitched_auto_launch_fast() -> None:
    assert ChatFastModeState(mode=FastModeMode.ON).launches_fast is True
    assert ChatFastModeState(mode=FastModeMode.AUTO).launches_fast is True
    assert ChatFastModeState(mode=FastModeMode.AUTO, is_switched=True).launches_fast is False
    assert ChatFastModeState(mode=FastModeMode.OFF).launches_fast is False


def test_the_fast_mode_file_reads_back_what_was_written(tmp_path: Path) -> None:
    state = ChatFastModeState(mode=FastModeMode.AUTO, is_switched=True)

    write_fast_mode_state(tmp_path / "chats" / "agent-1", state)

    assert read_fast_mode_state(tmp_path / "chats" / "agent-1") == state
    assert (tmp_path / "chats" / "agent-1" / FAST_MODE_FILENAME).is_file()


def test_a_chat_without_a_fast_mode_file_or_with_an_unreadable_one_reads_as_none(tmp_path: Path) -> None:
    assert read_fast_mode_state(tmp_path / "nowhere") is None
    (tmp_path / FAST_MODE_FILENAME).write_text("{not json")
    assert read_fast_mode_state(tmp_path) is None
    (tmp_path / FAST_MODE_FILENAME).write_text('{"mode": "faster"}')
    assert read_fast_mode_state(tmp_path) is None
