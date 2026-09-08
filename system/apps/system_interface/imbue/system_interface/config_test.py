"""Tests for the config module."""

from imbue.system_interface.config import Config
from imbue.system_interface.config import load_config


def test_default_config() -> None:
    config = Config()
    assert config.system_interface_host == "127.0.0.1"
    assert config.system_interface_port == 8000


def test_load_config_returns_config() -> None:
    config = load_config()
    assert isinstance(config, Config)
