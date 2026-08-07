"""Unit tests for the opencode startup-model probe."""

from pathlib import Path

from imbue.system_interface.harnesses.opencode.probe import read_server_port
from imbue.system_interface.harnesses.opencode.probe import startup_model_from_config


def test_startup_model_prefers_a_pinned_config_model() -> None:
    assert (
        startup_model_from_config({"model": "anthropic/claude-sonnet-4-5"}, {"default": {"opencode": "big-pickle"}})
        == "anthropic/claude-sonnet-4-5"
    )


def test_startup_model_falls_back_to_provider_default_when_unpinned() -> None:
    # This is the real case: mngr pins no model, so the startup model is the
    # authenticated provider's default from GET /config/providers.
    assert (
        startup_model_from_config({}, {"default": {"anthropic": "claude-sonnet-4-5"}}) == "anthropic/claude-sonnet-4-5"
    )


def test_startup_model_is_none_when_nothing_resolves() -> None:
    assert startup_model_from_config({}, {}) is None
    assert startup_model_from_config({}, {"default": {}}) is None


def test_read_server_port(tmp_path: Path) -> None:
    assert read_server_port(tmp_path) is None
    (tmp_path / "opencode_server_port").write_text("4096\n")
    assert read_server_port(tmp_path) == 4096


def test_read_server_port_blank_or_nonnumeric_is_none(tmp_path: Path) -> None:
    (tmp_path / "opencode_server_port").write_text("")
    assert read_server_port(tmp_path) is None
    (tmp_path / "opencode_server_port").write_text("nope")
    assert read_server_port(tmp_path) is None
