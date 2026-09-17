"""Tests for the chat app's entry point: the CLI args and the state they build."""

from imbue.chat.config import Config
from imbue.chat.main import MANIFEST_PATH
from imbue.chat.main import _parse_args
from imbue.chat.main import build_application
from imbue.chat.state import ChatAppState
from imbue.chat.state import state_of


def _built_state(argv: list[str]) -> ChatAppState:
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
