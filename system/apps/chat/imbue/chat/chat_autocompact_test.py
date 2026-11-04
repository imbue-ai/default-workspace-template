from pathlib import Path

from imbue.chat.chat_autocompact import AUTOCOMPACT_FILENAME
from imbue.chat.chat_autocompact import ChatAutocompactState
from imbue.chat.chat_autocompact import read_autocompact_state
from imbue.chat.chat_autocompact import write_autocompact_state


def test_the_autocompact_file_reads_back_what_was_written(tmp_path: Path) -> None:
    write_autocompact_state(tmp_path / "chats" / "agent-1", ChatAutocompactState(is_enabled=False))

    assert read_autocompact_state(tmp_path / "chats" / "agent-1") == ChatAutocompactState(is_enabled=False)
    assert (tmp_path / "chats" / "agent-1" / AUTOCOMPACT_FILENAME).is_file()

    write_autocompact_state(tmp_path / "chats" / "agent-1", ChatAutocompactState(is_enabled=True))

    assert read_autocompact_state(tmp_path / "chats" / "agent-1") == ChatAutocompactState(is_enabled=True)


def test_a_chat_without_an_autocompact_file_reads_as_none(tmp_path: Path) -> None:
    assert read_autocompact_state(tmp_path / "nowhere") is None


def test_an_unreadable_autocompact_file_warns_and_reads_as_none(tmp_path: Path, loguru_records: list[str]) -> None:
    (tmp_path / AUTOCOMPACT_FILENAME).write_text("{not json")
    assert read_autocompact_state(tmp_path) is None
    (tmp_path / AUTOCOMPACT_FILENAME).write_text('{"is_enabled": "sometimes"}')
    assert read_autocompact_state(tmp_path) is None

    warnings = [record for record in loguru_records if record.startswith("WARNING") and AUTOCOMPACT_FILENAME in record]
    assert len(warnings) == 2
