from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.chat.chat_settings import ChatSettings
from imbue.chat.chat_settings import ChatSettingsStore
from imbue.chat.chat_settings import DEFAULT_FAST_MODE_TURN_LIMIT
from imbue.chat.chat_settings import FastModeMode
from imbue.chat.chat_settings import SuggestionSettingsUpdate


def test_an_absent_settings_file_reads_as_the_defaults(tmp_path: Path) -> None:
    store = ChatSettingsStore(path=tmp_path / "settings.json")
    assert store.read() == ChatSettings(
        fast_mode_default=FastModeMode.OFF,
        fast_mode_turn_limit=DEFAULT_FAST_MODE_TURN_LIMIT,
        is_fast_mode_notice_shown=False,
        autocompact_default=True,
        is_autocompact_notice_shown=False,
        is_next_step_suggestions_enabled=True,
        is_new_chat_suggestions_enabled=True,
        is_picker_send_hint_shown=False,
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


def test_a_store_without_a_path_keeps_the_settings_in_memory() -> None:
    store = ChatSettingsStore(path=None)
    assert store.read() == ChatSettings()
    store.write(ChatSettings(fast_mode_turn_limit=1))
    assert store.read().fast_mode_turn_limit == 1


def test_a_file_from_before_suggestions_reads_with_every_placement_on(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        '{"fast_mode_default": "on", "fast_mode_turn_limit": 3, "is_fast_mode_notice_shown": true,'
        ' "autocompact_default": false, "is_autocompact_notice_shown": true}'
    )

    settings = ChatSettingsStore(path=path).read()

    assert settings.fast_mode_default is FastModeMode.ON and settings.autocompact_default is False
    assert settings.is_next_step_suggestions_enabled is True
    assert settings.is_new_chat_suggestions_enabled is True


def test_a_suggestion_switch_written_off_reads_back_off(tmp_path: Path) -> None:
    store = ChatSettingsStore(path=tmp_path / "settings.json")

    store.write(ChatSettings(is_new_chat_suggestions_enabled=False))

    assert store.read() == ChatSettings(
        is_next_step_suggestions_enabled=True,
        is_new_chat_suggestions_enabled=False,
    )


def test_a_suggestion_settings_update_changes_only_the_switches_it_names() -> None:
    settings = ChatSettings(
        fast_mode_default=FastModeMode.AUTO, fast_mode_turn_limit=4, is_new_chat_suggestions_enabled=False
    )

    updated = SuggestionSettingsUpdate(
        is_next_step_suggestions_enabled=False, is_new_chat_suggestions_enabled=True
    ).apply_to(settings)

    assert updated == ChatSettings(
        fast_mode_default=FastModeMode.AUTO,
        fast_mode_turn_limit=4,
        is_next_step_suggestions_enabled=False,
        is_new_chat_suggestions_enabled=True,
    )


def test_a_suggestion_settings_update_that_names_nothing_or_null_changes_nothing() -> None:
    settings = ChatSettings(is_new_chat_suggestions_enabled=False)

    assert SuggestionSettingsUpdate().apply_to(settings) == settings
    assert SuggestionSettingsUpdate.model_validate({"is_new_chat_suggestions_enabled": None}).apply_to(settings) == (
        settings
    )


def test_a_stored_file_with_a_field_the_settings_do_not_hold_keeps_its_other_values(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        '{"fast_mode_default": "on", "fast_mode_turn_limit": 5, "is_some_other_suggestions_enabled": false,'
        ' "is_next_step_suggestions_enabled": false, "a_setting_of_another_version": {"nested": [1, 2]}}'
    )
    store = ChatSettingsStore(path=path)

    settings = store.read()

    assert settings == ChatSettings(
        fast_mode_default=FastModeMode.ON, fast_mode_turn_limit=5, is_next_step_suggestions_enabled=False
    )
    assert set(settings.model_dump()) == set(ChatSettings.model_fields)
    # A change written over such a file keeps the values it held, too.
    updated = store.update(SuggestionSettingsUpdate(is_new_chat_suggestions_enabled=False).apply_to)
    assert updated == ChatSettings(
        fast_mode_default=FastModeMode.ON,
        fast_mode_turn_limit=5,
        is_next_step_suggestions_enabled=False,
        is_new_chat_suggestions_enabled=False,
    )


def test_a_suggestion_settings_update_carrying_an_unknown_switch_still_applies_the_known_ones() -> None:
    settings = ChatSettings(fast_mode_turn_limit=4)

    update = SuggestionSettingsUpdate.model_validate(
        {"is_some_other_suggestions_enabled": False, "is_new_chat_suggestions_enabled": False}
    )

    assert update == SuggestionSettingsUpdate(is_new_chat_suggestions_enabled=False)
    assert update.apply_to(settings) == ChatSettings(fast_mode_turn_limit=4, is_new_chat_suggestions_enabled=False)
    only_unknown = SuggestionSettingsUpdate.model_validate({"is_some_other_suggestions_enabled": False})
    assert only_unknown.apply_to(settings) == settings


def test_an_update_writes_the_change_over_the_settings_as_they_stand(tmp_path: Path) -> None:
    """The file is read at the update, not before it, so a change another writer made meanwhile is kept."""
    path = tmp_path / "settings.json"
    store = ChatSettingsStore(path=path)
    store.write(ChatSettings(fast_mode_turn_limit=1))
    ChatSettingsStore(path=path).write(ChatSettings(fast_mode_turn_limit=5))

    written = store.update(SuggestionSettingsUpdate(is_next_step_suggestions_enabled=False).apply_to)

    assert written == ChatSettings(fast_mode_turn_limit=5, is_next_step_suggestions_enabled=False)
    assert store.read() == written


def test_an_update_of_a_store_without_a_path_stays_in_memory() -> None:
    store = ChatSettingsStore(path=None)

    store.update(SuggestionSettingsUpdate(is_new_chat_suggestions_enabled=False).apply_to)

    assert store.read() == ChatSettings(is_new_chat_suggestions_enabled=False)
