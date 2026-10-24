from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.chat.chat_settings import ChatSettings
from imbue.chat.chat_settings import ChatSettingsStore
from imbue.chat.chat_settings import DEFAULT_FAST_MODE_TURN_LIMIT
from imbue.chat.chat_settings import DEFAULT_RETAINED_CHAT_LIMIT
from imbue.chat.chat_settings import FastModeMode


def test_an_absent_settings_file_reads_as_the_defaults(tmp_path: Path) -> None:
    store = ChatSettingsStore(path=tmp_path / "settings.json")
    assert store.read() == ChatSettings(
        fast_mode_default=FastModeMode.OFF,
        fast_mode_turn_limit=DEFAULT_FAST_MODE_TURN_LIMIT,
        retained_chat_limit=DEFAULT_RETAINED_CHAT_LIMIT,
        is_fast_mode_notice_shown=False,
        autocompact_default=True,
        is_autocompact_notice_shown=False,
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


def test_a_file_from_before_idle_compaction_reads_with_it_on_and_its_notice_unshown(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"fast_mode_default": "off", "fast_mode_turn_limit": 3, "is_fast_mode_notice_shown": true}')

    settings = ChatSettingsStore(path=path).read()

    assert settings.fast_mode_default is FastModeMode.OFF
    assert settings.fast_mode_turn_limit == 3
    assert settings.autocompact_default is True
    assert settings.is_autocompact_notice_shown is False


def test_a_turn_limit_below_one_is_refused() -> None:
    """Off is a mode of its own, so a limit of zero has no meaning left."""
    with pytest.raises(ValidationError):
        ChatSettings(fast_mode_turn_limit=0)


def test_retained_chat_limit_accepts_keep_all_and_rejects_invalid_counts() -> None:
    assert ChatSettings(retained_chat_limit=None).retained_chat_limit is None
    for value in (0, -1, 1.5, "12", True):
        with pytest.raises(ValidationError):
            ChatSettings.model_validate({"retained_chat_limit": value})


def test_partial_updates_preserve_other_preferences_across_store_instances(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    first = ChatSettingsStore(path=path)
    second = ChatSettingsStore(path=path)

    first.update({"retained_chat_limit": None})
    second.update({"fast_mode_default": "on"})

    assert first.read().retained_chat_limit is None
    assert first.read().fast_mode_default == FastModeMode.ON


def test_a_store_without_a_path_keeps_the_settings_in_memory() -> None:
    store = ChatSettingsStore(path=None)
    assert store.read() == ChatSettings()
    store.write(ChatSettings(fast_mode_turn_limit=1))
    assert store.read().fast_mode_turn_limit == 1
