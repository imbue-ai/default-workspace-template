from pathlib import Path

from app_instances.sidecar import app_url_port
from app_manifest.manifest import load_manifest

from terminal_app.main import APP_URL, INSTANCES_URL, MANIFEST_PATH, STORE_PATH

_REPO_ROOT = Path(__file__).resolve().parents[5]


def test_the_fixed_wiring_agrees_with_the_manifest() -> None:
    manifest = load_manifest(_REPO_ROOT / MANIFEST_PATH)

    assert manifest.instances_url == INSTANCES_URL
    assert manifest.name == "terminal"
    assert app_url_port(APP_URL) == 7681
    assert STORE_PATH == Path("data/.apps/terminal/instances.json")
