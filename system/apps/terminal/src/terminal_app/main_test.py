from pathlib import Path

from app_instances.sidecar import app_url_port
from app_manifest.manifest import load_manifest

from terminal_app import pty_main
from terminal_app.main import APP_URL, INSTANCES_URL, MANIFEST_PATH, STORE_PATH

_REPO_ROOT = Path(__file__).resolve().parents[5]


def test_the_fixed_wiring_agrees_with_the_manifest() -> None:
    manifest = load_manifest(_REPO_ROOT / MANIFEST_PATH)

    assert manifest.instances_url == INSTANCES_URL
    assert manifest.name == "terminal"
    assert app_url_port(APP_URL) == 7681
    assert STORE_PATH == Path("data/.apps/terminal/instances.json")


def test_the_pty_wiring_agrees_with_its_manifest() -> None:
    manifest = load_manifest(_REPO_ROOT / pty_main.MANIFEST_PATH)

    assert manifest.name == "terminal-pty"
    assert manifest.internal is True
    assert manifest.program == "terminal-pty"
    # Three ports, three programs' worth of listening: the pages, the instances API, and ttyd.
    assert len({app_url_port(APP_URL), app_url_port(INSTANCES_URL), app_url_port(pty_main.APP_URL)}) == 3
