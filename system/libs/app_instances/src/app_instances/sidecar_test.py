import sys
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from app_manifest.primitives import AppName, AppUrl, InstancesUrl
from app_manifest.registry import ENV_APPS_FILE, read_registry

from app_instances.errors import SidecarError
from app_instances.sidecar import (
    child_exit_code,
    register_app,
    run_sidecar,
    split_instances_url,
)
from app_instances.testing import (
    LOOPBACK_HOST,
    StubInstanceSource,
    free_port,
    is_port_accepting,
    write_sidecar_manifest,
)

# system/libs/app_instances/src/app_instances/sidecar_test.py -> the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[5]


def _unique_app_name() -> AppName:
    return AppName(f"sidecar-{uuid4().hex[:8]}")


@pytest.mark.parametrize(
    ("returncode", "expected"), [(0, 0), (3, 3), (-15, 143), (-9, 137)]
)
def test_child_exit_code_maps_a_signal_death_to_128_plus_the_signal(
    returncode: int, expected: int
) -> None:
    assert child_exit_code(returncode) == expected


def test_split_instances_url_names_the_loopback_host_and_port() -> None:
    assert split_instances_url(InstancesUrl("http://127.0.0.1:8301")) == (
        "127.0.0.1",
        8301,
    )
    assert split_instances_url(InstancesUrl("http://localhost:7682")) == (
        "localhost",
        7682,
    )


def test_run_sidecar_refuses_a_manifest_whose_instances_url_differs(
    tmp_path: Path,
) -> None:
    manifest_path = write_sidecar_manifest(
        tmp_path, _unique_app_name(), InstancesUrl("http://127.0.0.1:8301")
    )

    with pytest.raises(
        SidecarError, match="declares instances_url 'http://127.0.0.1:8301'"
    ):
        run_sidecar(
            manifest_path=manifest_path,
            app_url=AppUrl("http://localhost:8300"),
            instances_url=InstancesUrl("http://127.0.0.1:8302"),
            child_argv=[sys.executable, "-c", "pass"],
            source=StubInstanceSource(),
        )


def test_run_sidecar_refuses_a_single_instance_manifest(tmp_path: Path) -> None:
    (tmp_path / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    manifest_path = tmp_path / "app.toml"
    manifest_path.write_text(
        f'name = "{_unique_app_name()}"\ndisplay_name = "Single"\nicon = "icon.svg"\n'
    )

    with pytest.raises(SidecarError, match="does not declare instances = true"):
        run_sidecar(
            manifest_path=manifest_path,
            app_url=AppUrl("http://localhost:8300"),
            instances_url=InstancesUrl("http://127.0.0.1:8301"),
            child_argv=[sys.executable, "-c", "pass"],
            source=StubInstanceSource(),
        )


def test_run_sidecar_releases_its_port_when_registration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No registration script exists under this cwd, so registration fails after the listener is up.
    monkeypatch.chdir(tmp_path)
    port = free_port()
    instances_url = InstancesUrl(f"http://{LOOPBACK_HOST}:{port}")
    manifest_path = write_sidecar_manifest(tmp_path, _unique_app_name(), instances_url)

    with pytest.raises(SidecarError, match="registration script"):
        run_sidecar(
            manifest_path=manifest_path,
            app_url=AppUrl("http://localhost:8300"),
            instances_url=instances_url,
            child_argv=[sys.executable, "-c", "pass"],
            source=StubInstanceSource(),
        )

    assert not is_port_accepting(port)


def test_run_sidecar_refuses_to_run_off_the_main_thread(tmp_path: Path) -> None:
    instances_url = InstancesUrl("http://127.0.0.1:8301")
    manifest_path = write_sidecar_manifest(tmp_path, _unique_app_name(), instances_url)
    raised: list[BaseException] = []

    def run_in_thread() -> None:
        try:
            run_sidecar(
                manifest_path=manifest_path,
                app_url=AppUrl("http://localhost:8300"),
                instances_url=instances_url,
                child_argv=[sys.executable, "-c", "pass"],
                source=StubInstanceSource(),
            )
        except SidecarError as e:
            raised.append(e)

    worker = threading.Thread(target=run_in_thread)
    worker.start()
    worker.join(timeout=5)

    assert len(raised) == 1
    assert "main thread" in str(raised[0])


def test_register_app_writes_the_manifest_row_with_its_instances_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    registry_path = tmp_path / "apps.toml"
    monkeypatch.setenv(ENV_APPS_FILE, str(registry_path))
    app_name = _unique_app_name()
    instances_url = InstancesUrl("http://127.0.0.1:8301")
    manifest_path = write_sidecar_manifest(tmp_path, app_name, instances_url)

    register_app(manifest_path, AppUrl("http://localhost:8300"))

    rows = read_registry(registry_path)
    assert [row.name for row in rows] == [app_name]
    assert rows[0].url == "http://localhost:8300"
    assert rows[0].instances is True
    assert rows[0].instances_url == instances_url
    assert [action.id for action in rows[0].actions] == ["new"]


def test_register_app_reports_the_scripts_error_for_a_bad_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    monkeypatch.setenv(ENV_APPS_FILE, str(tmp_path / "apps.toml"))
    manifest_path = tmp_path / "app.toml"
    manifest_path.write_text('name = "Not A Name"\n')

    with pytest.raises(SidecarError, match="invalid app name"):
        register_app(manifest_path, AppUrl("http://localhost:8300"))
