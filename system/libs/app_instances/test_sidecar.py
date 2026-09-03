"""Integration: the sidecar as a real process around ``python -m http.server``."""

import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final
from uuid import uuid4

import httpx
import pytest
from app_instances.testing import (
    LOOPBACK_HOST,
    SidecarEnvironment,
    free_port,
    is_port_accepting,
    wait_until,
    write_sidecar_manifest,
)
from app_manifest.primitives import AppName, AppUrl, InstancesUrl
from app_manifest.registry import read_registry
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

_STARTUP_TIMEOUT_SECONDS: Final[float] = 20.0
_EXIT_TIMEOUT_SECONDS: Final[float] = 10.0


class _SidecarUnderTest(FrozenModel):
    """One sidecar process's command line, the ports and files it was given, and where its stderr lands."""

    app_name: AppName = Field(description="The unique name the sidecar registers")
    child_port: int = Field(
        description="The port the wrapped server is told to serve on"
    )
    instances_port: int = Field(description="The port the instances API is served on")
    instances_url: InstancesUrl = Field(description="Where the instances API is served")
    app_url: AppUrl = Field(description="The URL the app is registered at")
    registry_path: Path = Field(description="The apps.toml the registration lands in")
    store_path: Path = Field(
        description="The JSON store the sidecar keeps instances in"
    )
    log_path: Path = Field(description="Where the sidecar's stderr is captured")
    command: tuple[str, ...] = Field(description="The full sidecar command line")


def _prepare_sidecar(
    environment: SidecarEnvironment, child_argv: Sequence[str]
) -> _SidecarUnderTest:
    """Pick free ports, write the manifest, and assemble the sidecar command around ``child_argv`` (``{port}`` is the child's port)."""
    app_name = AppName(f"sidecar-{uuid4().hex[:8]}")
    child_port = free_port()
    instances_port = free_port()
    instances_url = InstancesUrl(f"http://{LOOPBACK_HOST}:{instances_port}")
    app_url = AppUrl(f"http://localhost:{child_port}")
    store_path = environment.scratch_dir / "instances.json"
    manifest_path = write_sidecar_manifest(
        environment.scratch_dir, app_name, instances_url
    )
    child_command = [
        argument.replace("{port}", str(child_port)) for argument in child_argv
    ]
    return _SidecarUnderTest(
        app_name=app_name,
        child_port=child_port,
        instances_port=instances_port,
        instances_url=instances_url,
        app_url=app_url,
        registry_path=environment.registry_path,
        store_path=store_path,
        log_path=environment.scratch_dir / "sidecar.log",
        command=(
            sys.executable,
            "-m",
            "app_instances.testing",
            "sidecar",
            "--manifest",
            str(manifest_path),
            "--app-url",
            app_url,
            "--instances-url",
            instances_url,
            "--store",
            str(store_path),
            "--",
            *child_command,
        ),
    )


def _read_log(sidecar: _SidecarUnderTest) -> str:
    return sidecar.log_path.read_text() if sidecar.log_path.exists() else ""


def _spawn(sidecar: _SidecarUnderTest) -> subprocess.Popen[bytes]:
    with sidecar.log_path.open("wb") as log_file:
        return subprocess.Popen(
            sidecar.command, stdout=subprocess.DEVNULL, stderr=log_file
        )


@pytest.mark.timeout(60)
def test_sidecar_registers_serves_instances_and_forwards_sigterm_to_the_child(
    sidecar_environment: SidecarEnvironment,
) -> None:
    served_dir = sidecar_environment.scratch_dir / "served"
    served_dir.mkdir()
    (served_dir / "hello.txt").write_text("hello from the wrapped server")
    sidecar = _prepare_sidecar(
        sidecar_environment,
        [
            sys.executable,
            "-m",
            "http.server",
            "{port}",
            "--bind",
            LOOPBACK_HOST,
            "--directory",
            str(served_dir),
        ],
    )
    process = _spawn(sidecar)
    try:
        # The instances API listens before the app is registered, and the child starts after.
        assert wait_until(
            lambda: sidecar.registry_path.exists(), _STARTUP_TIMEOUT_SECONDS
        ), _read_log(sidecar)
        assert is_port_accepting(sidecar.instances_port), _read_log(sidecar)
        rows = read_registry(sidecar.registry_path)
        assert [row.name for row in rows] == [sidecar.app_name]
        assert rows[0].url == sidecar.app_url
        assert rows[0].instances is True
        assert rows[0].instances_url == sidecar.instances_url

        listed = httpx.get(f"{sidecar.instances_url}/_instances", timeout=5.0)
        assert listed.status_code == 200
        assert listed.json() == {"instances": []}
        created = httpx.post(
            f"{sidecar.instances_url}/_instances",
            json={"action": "new", "params": {"path": "/hello.txt"}},
            timeout=5.0,
        )
        assert created.status_code == 201, created.text
        assert created.json()["instance"]["key"] == f"{sidecar.app_name}-1"
        assert created.json()["instance"]["url"] == "/hello.txt"
        assert created.json()["instance"]["title"] == f"Sidecar {sidecar.app_name} 1"
        assert created.json()["instance"]["lifetime"] == "referenced"
        assert sidecar.store_path.exists()

        assert wait_until(
            lambda: is_port_accepting(sidecar.child_port), _STARTUP_TIMEOUT_SECONDS
        ), _read_log(sidecar)
        assert (
            httpx.get(f"{sidecar.app_url}/hello.txt", timeout=5.0).text
            == "hello from the wrapped server"
        )

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=_EXIT_TIMEOUT_SECONDS) == 143, _read_log(sidecar)
        assert not is_port_accepting(sidecar.child_port)
        assert not is_port_accepting(sidecar.instances_port)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.timeout(60)
def test_sidecar_exits_with_the_childs_own_exit_code(
    sidecar_environment: SidecarEnvironment,
) -> None:
    sidecar = _prepare_sidecar(
        sidecar_environment, [sys.executable, "-c", "import sys; sys.exit(3)"]
    )
    process = _spawn(sidecar)
    try:
        assert process.wait(timeout=_STARTUP_TIMEOUT_SECONDS) == 3, _read_log(sidecar)
        assert [row.name for row in read_registry(sidecar.registry_path)] == [
            sidecar.app_name
        ]
        assert not is_port_accepting(sidecar.instances_port)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
