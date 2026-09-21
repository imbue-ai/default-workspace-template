"""Tests for the entry point's wiring: the arguments the config yields, and the page app it builds."""

from pathlib import Path

from getting_started.config import Config
from getting_started.main import APP_NAME
from getting_started.main import MANIFEST_PATH
from getting_started.main import arguments_from_config
from getting_started.main import build_first_window_opener
from getting_started.main import build_pages_app


def test_arguments_come_from_the_config_and_the_flags(tmp_path: Path) -> None:
    config = Config(getting_started_port=8123, system_interface_template_catalog_url="")
    arguments = arguments_from_config(config, MANIFEST_PATH, tmp_path / "state", tmp_path / "static")

    assert arguments.app_url == "http://localhost:8123"
    assert arguments.host == "127.0.0.1"
    assert arguments.catalog_url == ""
    assert arguments.state_dir == tmp_path / "state"


def test_the_page_app_serves_the_health_probe_and_the_opener_is_wired_to_the_state_directory(tmp_path: Path) -> None:
    config = Config(system_interface_template_catalog_url="")
    arguments = arguments_from_config(config, MANIFEST_PATH, tmp_path / "state", tmp_path / "static")

    app = build_pages_app(arguments)
    assert app.test_client().get("/api/health").get_json() == {"status": "ok", "is_frontend_built": False}
    assert app.test_client().get("/api/templates-catalog").get_json() == {"catalog": None, "is_stale": False}

    opener = build_first_window_opener(arguments)
    assert opener.app == APP_NAME
    assert opener.ledger.path == tmp_path / "state" / "first_window.json"
