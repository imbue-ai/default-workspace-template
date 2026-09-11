"""Tests for the chat app's entry point: the CLI args and the state they build."""

from pathlib import Path

from imbue.chat.config import Config
from imbue.chat.main import MANIFEST_PATH
from imbue.chat.main import _parse_args
from imbue.chat.main import build_application
from imbue.chat.main import build_production_state
from imbue.chat.message_stamps import STAMPS_FILENAME
from imbue.chat.state import ChatState
from imbue.chat.state import state_of


def _built_state(argv: list[str]) -> ChatState:
    return state_of(build_application(Config(), _parse_args(argv)))


def test_build_application_defaults_have_no_filters() -> None:
    """With no CLI filter args, the app carries no provider/include/exclude filters."""
    state = _built_state([])
    try:
        assert state.provider_names is None
        assert state.include_filters == ()
        assert state.exclude_filters == ()
    finally:
        state.shutdown()


def test_build_application_threads_filters_through() -> None:
    """CLI filter args reach the app's state as provider/include/exclude filters."""
    state = _built_state(
        [
            "--provider",
            "local",
            "--include",
            'state == "RUNNING"',
            "--exclude",
            'name == "test"',
        ]
    )
    try:
        assert state.provider_names == ("local",)
        assert state.include_filters == ('state == "RUNNING"',)
        assert state.exclude_filters == ('name == "test"',)
    finally:
        state.shutdown()


def test_registration_defaults_to_the_manifest_and_is_skippable() -> None:
    """A plain boot registers the chat's manifest; ``--no-register`` is for a throwaway boot."""
    assert _parse_args([]).manifest == MANIFEST_PATH
    assert _parse_args([]).no_register is False
    assert _parse_args(["--no-register"]).no_register is True


def test_preflight_is_off_by_default_and_a_flag_of_its_own() -> None:
    """``--preflight`` is the update apply's throwaway boot; a plain boot never takes it."""
    assert _parse_args([]).preflight is False
    assert _parse_args(["--preflight"]).preflight is True


def test_secondary_is_off_by_default_and_names_its_own_shell() -> None:
    """``--secondary`` is a preview's boot; the shell it nudges is the one named beside it, else none."""
    assert _parse_args([]).secondary is False
    assert _parse_args([]).nudge_shell_url == ""
    secondary = _parse_args(["--secondary", "--nudge-shell-url", "http://127.0.0.1:9"])
    assert secondary.secondary is True
    assert secondary.nudge_shell_url == "http://127.0.0.1:9"


def test_message_stamps_land_in_the_configured_data_dir(tmp_path: Path) -> None:
    """The chat's data directory is the config's: a secondary chat pointed at a scratch copy writes nowhere else."""
    state = build_production_state(Config(chat_data_dir=tmp_path / "scratch"), is_secondary=True)
    try:
        state.agent_manager.record_message_sent("agent-stamped")
        assert (tmp_path / "scratch" / STAMPS_FILENAME).exists()
    finally:
        state.shutdown()
