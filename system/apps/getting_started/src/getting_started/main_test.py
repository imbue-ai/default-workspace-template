"""Tests for the entry point's wiring: the arguments the config yields, the page app it builds, and what an
unregistered boot leaves alone."""

import socket
import threading
import urllib.request
from pathlib import Path

import pytest

from app_manifest.registry import ENV_APPS_FILE
from getting_started.config import Config
from getting_started.first_window import LEDGER_FILENAME
from getting_started.first_window import OPENER_THREAD_NAME
from getting_started.main import APP_NAME
from getting_started.main import GettingStartedArguments
from getting_started.main import MANIFEST_PATH
from getting_started.main import arguments_from_config
from getting_started.main import build_first_window_opener
from getting_started.main import build_pages_app
from getting_started.main import run_getting_started_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _arguments(tmp_path: Path, *, port: int, is_registered: bool) -> GettingStartedArguments:
    """The arguments a config with no catalog yields, over state and static directories under ``tmp_path``."""
    config = Config(getting_started_port=port, system_interface_template_catalog_url="")
    return arguments_from_config(
        config, MANIFEST_PATH, tmp_path / "state", tmp_path / "static", is_registered=is_registered
    )


def test_arguments_come_from_the_config_and_the_flags(tmp_path: Path) -> None:
    arguments = _arguments(tmp_path, port=8123, is_registered=True)

    assert arguments.app_url == "http://localhost:8123"
    assert arguments.host == "127.0.0.1"
    assert arguments.catalog_url == ""
    assert arguments.state_dir == tmp_path / "state"
    assert arguments.is_registered is True
    assert _arguments(tmp_path, port=8123, is_registered=False).is_registered is False


def test_the_page_app_serves_the_health_probe_and_the_opener_is_wired_to_the_state_directory(tmp_path: Path) -> None:
    arguments = _arguments(tmp_path, port=8123, is_registered=True)

    app = build_pages_app(arguments)
    assert app.test_client().get("/api/health").get_json() == {"status": "ok", "is_frontend_built": False}
    assert app.test_client().get("/api/templates-catalog").get_json() == {"catalog": None, "is_stale": False}

    opener = build_first_window_opener(arguments)
    assert opener.app == APP_NAME
    assert opener.ledger.path == tmp_path / "state" / LEDGER_FILENAME


def test_an_unregistered_run_serves_the_page_but_neither_registers_nor_opens_the_first_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview's boot: the page answers on its port, while the live registry row and the live desktop are
    left alone."""
    registry_path = tmp_path / "apps.toml"
    monkeypatch.setenv(ENV_APPS_FILE, str(registry_path))
    port = _free_port()
    arguments = _arguments(tmp_path, port=port, is_registered=False)
    seen: dict[str, object] = {}

    def observe_and_stop() -> int:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as response:
            seen["health_status"] = response.status
        seen["opener_threads"] = [thread.name for thread in threading.enumerate() if thread.name == OPENER_THREAD_NAME]
        return 130

    assert run_getting_started_app(arguments, observe_and_stop) == 130

    assert seen == {"health_status": 200, "opener_threads": []}
    assert not registry_path.exists()
    assert not (tmp_path / "state" / LEDGER_FILENAME).exists()
