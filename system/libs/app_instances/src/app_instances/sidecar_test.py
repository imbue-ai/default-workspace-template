import sys
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from app_manifest.manifest import AppManifest
from app_manifest.primitives import AppName, AppUrl, InstancesUrl
from app_manifest.registry import read_registry
from flask import Flask

from app_instances.blueprint import build_instances_app
from app_instances.errors import SidecarError
from app_instances.interfaces import InstanceNudgerInterface
from app_instances.sidecar import (
    app_url_port,
    child_exit_code,
    register_app,
    run_sidecar,
    run_sidecar_app,
    serve_in_background,
    split_instances_url,
)
from app_instances.testing import (
    LOOPBACK_HOST,
    RecordingNudger,
    SidecarEnvironment,
    StubInstanceSource,
    free_port,
    is_port_accepting,
    write_sidecar_manifest,
)


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


def _write_manifest(directory: Path, manifest_tail: str) -> Path:
    """Write a manifest with the common name, display name, and icon, ending in ``manifest_tail``."""
    (directory / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    manifest_path = directory / "app.toml"
    manifest_path.write_text(
        f'name = "{_unique_app_name()}"\ndisplay_name = "Sidecar"\nicon = "icon.svg"\n'
        + manifest_tail
    )
    return manifest_path


def _run_sidecar_around_a_noop_child(
    manifest_path: Path, instances_url: InstancesUrl
) -> int:
    """Run the sidecar over a stub source around a child that exits at once."""
    return run_sidecar(
        manifest_path=manifest_path,
        app_url=AppUrl("http://localhost:8300"),
        instances_url=instances_url,
        child_argv=[sys.executable, "-c", "pass"],
        source=StubInstanceSource(),
    )


@pytest.mark.parametrize(
    ("manifest_tail", "expected_problem"),
    [
        (
            'instances = true\ninstances_url = "http://127.0.0.1:8301"\n',
            "declares instances_url 'http://127.0.0.1:8301'",
        ),
        (
            "instances = true\n",
            "declares no instances_url; a sidecar needs instances_url = 'http://127.0.0.1:8302'",
        ),
        ("", "does not declare instances = true"),
    ],
)
def test_run_sidecar_refuses_a_manifest_that_does_not_fit_the_served_url(
    tmp_path: Path, manifest_tail: str, expected_problem: str
) -> None:
    manifest_path = _write_manifest(tmp_path, manifest_tail)

    with pytest.raises(SidecarError, match=expected_problem):
        _run_sidecar_around_a_noop_child(
            manifest_path, InstancesUrl("http://127.0.0.1:8302")
        )


def test_run_sidecar_refuses_an_empty_child_command(tmp_path: Path) -> None:
    instances_url = InstancesUrl("http://127.0.0.1:8301")
    manifest_path = write_sidecar_manifest(tmp_path, _unique_app_name(), instances_url)

    with pytest.raises(SidecarError, match="no command given"):
        run_sidecar(
            manifest_path=manifest_path,
            app_url=AppUrl("http://localhost:8300"),
            instances_url=instances_url,
            child_argv=[],
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
        _run_sidecar_around_a_noop_child(manifest_path, instances_url)

    assert not is_port_accepting(port)


def test_run_sidecar_refuses_to_run_off_the_main_thread(tmp_path: Path) -> None:
    instances_url = InstancesUrl("http://127.0.0.1:8301")
    manifest_path = write_sidecar_manifest(tmp_path, _unique_app_name(), instances_url)
    raised: list[BaseException] = []

    def run_in_thread() -> None:
        try:
            _run_sidecar_around_a_noop_child(manifest_path, instances_url)
        except SidecarError as e:
            raised.append(e)

    worker = threading.Thread(target=run_in_thread)
    worker.start()
    worker.join(timeout=5)

    assert len(raised) == 1
    assert "main thread" in str(raised[0])


def test_register_app_writes_the_manifest_row_with_its_instances_url(
    sidecar_environment: SidecarEnvironment,
) -> None:
    app_name = _unique_app_name()
    instances_url = InstancesUrl("http://127.0.0.1:8301")
    manifest_path = write_sidecar_manifest(
        sidecar_environment.scratch_dir, app_name, instances_url
    )

    register_app(manifest_path, AppUrl("http://localhost:8300"))

    rows = read_registry(sidecar_environment.registry_path)
    assert [row.name for row in rows] == [app_name]
    assert rows[0].url == "http://localhost:8300"
    assert rows[0].instances is True
    assert rows[0].instances_url == instances_url
    assert [action.id for action in rows[0].actions] == ["new"]


def test_register_app_reports_the_scripts_error_for_a_bad_manifest(
    sidecar_environment: SidecarEnvironment,
) -> None:
    manifest_path = sidecar_environment.scratch_dir / "app.toml"
    manifest_path.write_text('name = "Not A Name"\n')

    with pytest.raises(SidecarError, match="invalid app name"):
        register_app(manifest_path, AppUrl("http://localhost:8300"))


def test_serve_in_background_accepts_while_entered_and_releases_the_port_on_exit() -> (
    None
):
    port = free_port()
    app = build_instances_app(StubInstanceSource(), RecordingNudger())

    with serve_in_background(LOOPBACK_HOST, port, app):
        assert is_port_accepting(port)
        with pytest.raises(SidecarError, match="cannot bind"):
            with serve_in_background(LOOPBACK_HOST, port, app):
                pass

    assert not is_port_accepting(port)


def test_run_sidecar_app_serves_the_routes_the_app_mounts_beside_the_blueprint(
    sidecar_environment: SidecarEnvironment,
) -> None:
    instances_port = free_port()
    instances_url = InstancesUrl(f"http://{LOOPBACK_HOST}:{instances_port}")
    manifest_path = write_sidecar_manifest(
        sidecar_environment.scratch_dir, _unique_app_name(), instances_url
    )
    seen_manifest_names: list[str] = []

    def build_app_with_an_extra_route(
        manifest: AppManifest, nudger: InstanceNudgerInterface
    ) -> Flask:
        seen_manifest_names.append(manifest.name)
        app = build_instances_app(StubInstanceSource(), nudger)

        @app.get("/extra")
        def extra() -> str:
            return "extra route"

        return app

    # The child proves the extra route is served while it runs: urlopen raises on a 404, and
    # the interpreter then exits non-zero.
    exit_code = run_sidecar_app(
        manifest_path=manifest_path,
        app_url=AppUrl("http://localhost:8300"),
        instances_url=instances_url,
        child_argv=[
            sys.executable,
            "-c",
            "import urllib.request; "
            f"body = urllib.request.urlopen('{instances_url}/extra').read(); "
            "raise SystemExit(0 if body == b'extra route' else 2)",
        ],
        build_app=build_app_with_an_extra_route,
    )

    assert exit_code == 0
    assert seen_manifest_names == [
        row.name for row in read_registry(sidecar_environment.registry_path)
    ]


def test_app_url_port_reads_the_wrapped_servers_port_from_the_app_url() -> None:
    assert app_url_port(AppUrl("http://localhost:8080")) == 8080
    with pytest.raises(SidecarError, match="names no port"):
        app_url_port(AppUrl("http://localhost"))
    with pytest.raises(SidecarError, match="names no usable port"):
        app_url_port(AppUrl("http://localhost:seven"))
