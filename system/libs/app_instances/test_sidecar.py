"""Integration: the sidecar as a real process around ``python -m http.server``."""

import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from app_instances.nudge import ENV_SHELL_URL
from app_instances.testing import (
    LOOPBACK_HOST,
    free_port,
    is_port_accepting,
    wait_until,
    write_sidecar_manifest,
)
from app_manifest.primitives import AppName, InstancesUrl
from app_manifest.registry import ENV_APPS_FILE, read_registry

# system/libs/app_instances/test_sidecar.py -> the repository root, where forward_port.py lives.
_REPO_ROOT = Path(__file__).resolve().parents[3]

_STARTUP_TIMEOUT_SECONDS = 20.0
_EXIT_TIMEOUT_SECONDS = 10.0


class _SidecarUnderTest:
    """One sidecar process, the ports and files it was given, and its captured stderr."""

    def __init__(self, tmp_path: Path, child_argv: list[str]) -> None:
        self.app_name = AppName(f"sidecar-{uuid4().hex[:8]}")
        self.child_port = free_port()
        self.instances_port = free_port()
        self.instances_url = InstancesUrl(
            f"http://{LOOPBACK_HOST}:{self.instances_port}"
        )
        self.app_url = f"http://localhost:{self.child_port}"
        self.registry_path = tmp_path / "apps.toml"
        self.store_path = tmp_path / "instances.json"
        self.log_path = tmp_path / "sidecar.log"
        manifest_path = write_sidecar_manifest(
            tmp_path, self.app_name, self.instances_url
        )
        self.child_argv = [
            argument.replace("{port}", str(self.child_port)) for argument in child_argv
        ]
        self.command = [
            sys.executable,
            "-m",
            "app_instances.testing",
            "sidecar",
            "--manifest",
            str(manifest_path),
            "--app-url",
            self.app_url,
            "--instances-url",
            self.instances_url,
            "--store",
            str(self.store_path),
            "--",
            *self.child_argv,
        ]

    def log(self) -> str:
        return self.log_path.read_text() if self.log_path.exists() else ""


@pytest.fixture
def sidecar_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """The cwd, registry, and (unreachable) shell every sidecar process in this file runs against."""
    monkeypatch.chdir(_REPO_ROOT)
    monkeypatch.setenv(ENV_APPS_FILE, str(tmp_path / "apps.toml"))
    monkeypatch.setenv(ENV_SHELL_URL, f"http://{LOOPBACK_HOST}:{free_port()}")
    yield tmp_path


def _spawn(sidecar: _SidecarUnderTest) -> subprocess.Popen[bytes]:
    with sidecar.log_path.open("wb") as log_file:
        return subprocess.Popen(
            sidecar.command, stdout=subprocess.DEVNULL, stderr=log_file
        )


@pytest.mark.timeout(60)
def test_sidecar_registers_serves_instances_and_forwards_sigterm_to_the_child(
    sidecar_environment: Path,
) -> None:
    served_dir = sidecar_environment / "served"
    served_dir.mkdir()
    (served_dir / "hello.txt").write_text("hello from the wrapped server")
    sidecar = _SidecarUnderTest(
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
        ), sidecar.log()
        assert is_port_accepting(sidecar.instances_port), sidecar.log()
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
        ), sidecar.log()
        assert (
            httpx.get(f"{sidecar.app_url}/hello.txt", timeout=5.0).text
            == "hello from the wrapped server"
        )

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=_EXIT_TIMEOUT_SECONDS) == 143, sidecar.log()
        assert not is_port_accepting(sidecar.child_port)
        assert not is_port_accepting(sidecar.instances_port)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.timeout(60)
def test_sidecar_exits_with_the_childs_own_exit_code(sidecar_environment: Path) -> None:
    sidecar = _SidecarUnderTest(
        sidecar_environment, [sys.executable, "-c", "import sys; sys.exit(3)"]
    )
    process = _spawn(sidecar)
    try:
        assert process.wait(timeout=_STARTUP_TIMEOUT_SECONDS) == 3, sidecar.log()
        assert [row.name for row in read_registry(sidecar.registry_path)] == [
            sidecar.app_name
        ]
        assert not is_port_accepting(sidecar.instances_port)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
