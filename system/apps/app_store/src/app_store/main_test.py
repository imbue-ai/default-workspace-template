"""The entry point's wiring: the arguments the config yields, the engine and page app it builds, and that an
unregistered boot (a preview) serves the page but neither registers nor runs installs."""

import socket
import urllib.request
from pathlib import Path

import pytest

from app_manifest.registry_location import ENV_APPS_FILE
from app_store.config import Config
from app_store.main import AppStoreArguments
from app_store.main import MANIFEST_PATH
from app_store.main import arguments_from_config
from app_store.main import build_engine
from app_store.main import build_pages_app
from app_store.main import read_registry_rows
from app_store.main import run_app_store
from app_store.testing import build_test_adder
from app_store.testing import build_test_engine


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _arguments(tmp_path: Path, port: int, is_registered: bool) -> AppStoreArguments:
    config = Config(
        app_store_port=port,
        app_store_data_dir=tmp_path / "data",
        app_store_state_dir=tmp_path / "state",
        app_store_template_catalog_url="",
    )
    return arguments_from_config(config, MANIFEST_PATH, tmp_path / "static", is_registered=is_registered)


def test_arguments_come_from_the_config_and_the_flags(tmp_path: Path) -> None:
    arguments = _arguments(tmp_path, 8123, is_registered=True)
    assert arguments.app_url == "http://localhost:8123"
    assert (arguments.host, arguments.catalog_url) == ("127.0.0.1", "")
    assert (arguments.data_dir, arguments.state_dir) == (tmp_path / "data", tmp_path / "state")
    assert _arguments(tmp_path, 8123, is_registered=False).is_registered is False


def test_the_production_engine_is_built_over_the_data_folder_and_the_shipped_catalog(tmp_path: Path) -> None:
    engine = build_engine(_arguments(tmp_path, 8123, is_registered=True), lambda: [])
    assert engine.layout.data_dir == (tmp_path / "data").absolute()
    assert len(engine.catalog.apps) == 7
    assert engine.records.read().records == ()


def test_the_page_app_serves_the_health_probe_and_the_store(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    app = build_pages_app(_arguments(tmp_path, 8123, is_registered=True), engine, build_test_adder(engine, tmp_path))
    client = app.test_client()
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": False}
    assert len(client.get("/api/store").get_json()["apps"]) == 7
    assert client.get("/api/templates-catalog").get_json() == {"catalog": None, "is_stale": False}


def test_an_unregistered_boot_serves_without_registering_or_running_installs(tmp_path: Path) -> None:
    port = _free_port()
    engine = build_test_engine(tmp_path)
    engine.request_install("datasette")
    answered: list[int] = []

    def wait_for_shutdown() -> int:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as response:
            answered.append(response.status)
        return 0

    assert run_app_store(_arguments(tmp_path, port, is_registered=False), engine, wait_for_shutdown) == 0
    assert answered == [200]
    # No worker ran the queued install.
    record = engine.records.read().find("datasette")
    assert record is not None and record.status.value == "QUEUED"


def test_an_unreadable_registry_reads_as_no_apps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = tmp_path / "apps.toml"
    broken.write_text("[[apps]\n")
    monkeypatch.setenv(ENV_APPS_FILE, str(broken))
    assert read_registry_rows() == []
