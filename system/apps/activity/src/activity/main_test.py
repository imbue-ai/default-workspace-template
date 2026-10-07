"""Tests for the entry point's wiring: the arguments the config yields, and what an unregistered boot leaves alone."""

import socket
import urllib.request
from pathlib import Path

import httpx
import pytest

from activity.config import Config
from activity.main import ActivityArguments
from activity.main import MANIFEST_PATH
from activity.main import arguments_from_config
from activity.main import build_pages_app
from activity.main import run_activity_app
from app_manifest.registry import ENV_APPS_FILE


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _arguments(tmp_path: Path, port: int, is_registered: bool) -> ActivityArguments:
    return arguments_from_config(
        config=Config(activity_port=port),
        manifest_path=MANIFEST_PATH,
        static_directory=tmp_path / "static",
        is_registered=is_registered,
    )


def test_arguments_come_from_the_config_and_the_flags(tmp_path: Path) -> None:
    arguments = _arguments(tmp_path, port=8123, is_registered=True)
    assert (arguments.app_url, arguments.host, arguments.is_registered) == ("http://localhost:8123", "127.0.0.1", True)


def test_the_page_app_serves_the_health_probe(tmp_path: Path) -> None:
    with httpx.Client() as client:
        app = build_pages_app(_arguments(tmp_path, port=8123, is_registered=True), client)
        assert app.test_client().get("/api/health").get_json() == {"status": "ok", "is_frontend_built": False}


def test_an_unregistered_run_serves_the_page_without_touching_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "apps.toml"
    monkeypatch.setenv(ENV_APPS_FILE, str(registry_path))
    port = _free_port()
    seen: dict[str, int] = {}

    def observe_and_stop() -> int:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as response:
            seen["health_status"] = response.status
        return 130

    assert run_activity_app(_arguments(tmp_path, port=port, is_registered=False), observe_and_stop) == 130
    assert seen == {"health_status": 200}
    assert not registry_path.exists()
