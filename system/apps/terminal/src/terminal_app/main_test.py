from pathlib import Path

import pytest
from app_manifest.manifest import load_manifest
from app_manifest.primitives import AppUrl

from terminal_app.errors import TerminalAppError
from terminal_app.main import APP_URL, INSTANCES_URL, MANIFEST_PATH, ttyd_port

_REPO_ROOT = Path(__file__).resolve().parents[5]


def test_the_fixed_wiring_agrees_with_the_manifest() -> None:
    manifest = load_manifest(_REPO_ROOT / MANIFEST_PATH)

    assert manifest.instances_url == INSTANCES_URL
    assert manifest.name == "terminal"
    assert ttyd_port(APP_URL) == 7681


def test_ttyd_port_needs_a_numeric_port_in_the_app_url() -> None:
    assert ttyd_port(AppUrl("http://localhost:8080")) == 8080
    with pytest.raises(TerminalAppError, match="names no port"):
        ttyd_port(AppUrl("http://localhost"))
    with pytest.raises(TerminalAppError, match="names no usable port"):
        ttyd_port(AppUrl("http://localhost:seven"))
