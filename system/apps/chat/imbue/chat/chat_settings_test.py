from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.chat.chat_settings import ChatSettings
from imbue.chat.chat_settings import ChatSettingsStore
from imbue.chat.chat_settings import DEFAULT_FAST_MODE_TURN_LIMIT
from imbue.chat.chat_settings import FastModeMode


def test_an_absent_settings_file_reads_as_the_defaults(tmp_path: Path) -> None:
    store = ChatSettingsStore(path=tmp_path / "settings.json")
    assert store.read() == ChatSettings(
        fast_mode_default=FastModeMode.AUTO,
        fast_mode_turn_limit=DEFAULT_FAST_MODE_TURN_LIMIT,
        is_fast_mode_notice_shown=False,
    )


def test_a_write_is_what_the_next_read_returns(tmp_path: Path) -> None:
    store = ChatSettingsStore(path=tmp_path / "chat" / "settings.json")
    written = ChatSettings(fast_mode_default=FastModeMode.OFF, fast_mode_turn_limit=1, is_fast_mode_notice_shown=True)

    store.write(written)

    assert store.read() == written
    # Another store over the same file (another process, in effect) sees the same.
    assert ChatSettingsStore(path=tmp_path / "chat" / "settings.json").read() == written


def test_an_unreadable_settings_file_reads_as_the_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    assert ChatSettingsStore(path=path).read() == ChatSettings()
    path.write_text('{"fast_mode_turn_limit": 0}')
    assert ChatSettingsStore(path=path).read() == ChatSettings()
    path.write_text('{"fast_mode_default": "sometimes"}')
    assert ChatSettingsStore(path=path).read() == ChatSettings()


def test_an_older_file_missing_a_field_takes_that_fields_default(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"fast_mode_turn_limit": 2}')
    assert ChatSettingsStore(path=path).read() == ChatSettings(fast_mode_turn_limit=2, is_fast_mode_notice_shown=False)


def test_a_turn_limit_below_one_is_refused() -> None:
    """Off is a mode of its own, so a limit of zero has no meaning left."""
    with pytest.raises(ValidationError):
        ChatSettings(fast_mode_turn_limit=0)


def test_a_store_without_a_path_keeps_the_settings_in_memory() -> None:
    store = ChatSettingsStore(path=None)
    assert store.read() == ChatSettings()
    store.write(ChatSettings(fast_mode_turn_limit=1))
    assert store.read().fast_mode_turn_limit == 1
