from pathlib import Path

from app_instances.data_types import InstanceLifetime
from app_instances.sidecar import app_url_port
from app_manifest.manifest import load_manifest
from inline_snapshot import snapshot

from files_app.main import (
    APP_NAME,
    APP_URL,
    INSTANCES_URL,
    MANIFEST_PATH,
    STORE_PATH,
    build_dufs_argv,
    build_files_source,
)

_REPO_ROOT = Path(__file__).resolve().parents[5]


def test_the_fixed_wiring_agrees_with_the_manifest() -> None:
    manifest = load_manifest(_REPO_ROOT / MANIFEST_PATH)

    assert manifest.instances_url == INSTANCES_URL
    assert manifest.name == APP_NAME
    assert app_url_port(APP_URL) == 8300
    assert STORE_PATH == Path("data/.apps/files/instances.json")


def test_the_source_is_wired_as_the_files_row_of_the_contract(tmp_path: Path) -> None:
    source = build_files_source(tmp_path / "instances.json")

    assert source.key_prefix == "files"
    assert source.title_template == "File Viewer {n}"
    assert source.lifetime is InstanceLifetime.REFERENCED
    assert source.is_renameable is False
    assert source.is_location_tracked is True


def test_the_dufs_command_line_is_the_one_supervisord_used_to_run() -> None:
    assert build_dufs_argv(dufs_executable="dufs", port=8300) == snapshot(
        [
            "dufs",
            "--allow-all",
            "--bind",
            "127.0.0.1",
            "--port",
            "8300",
            "--assets",
            "system/apps/files/assets",
            "data",
        ]
    )
