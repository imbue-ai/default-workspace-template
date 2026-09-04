"""Integration: ``files-app`` as a real process around a fake dufs."""

import json
import os
import signal
import subprocess
import sys
from collections.abc import Mapping
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
from app_manifest.primitives import AppName
from app_manifest.registry import read_registry
from files_app.testing import ENV_FAKE_DUFS_DIR, install_fake_dufs, read_fake_dufs_argv
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

_STARTUP_TIMEOUT_SECONDS: Final[float] = 20.0
_EXIT_TIMEOUT_SECONDS: Final[float] = 10.0
_REQUEST_TIMEOUT_SECONDS: Final[float] = 5.0


class _FilesAppUnderTest(FrozenModel):
    """One files-app process's command line, its ports and files, and where its stderr lands."""

    app_name: AppName = Field(description="The unique name the app registers")
    dufs_port: int = Field(description="The port the fake dufs is told to serve on")
    instances_port: int = Field(description="The port the instances API is served on")
    instances_url: str = Field(description="Where the instances API is served")
    store_path: Path = Field(description="The instances.json the app is told to use")
    dufs_record_dir: Path = Field(description="Where the fake dufs records its argv")
    log_path: Path = Field(description="Where the app's stderr is captured")
    command: tuple[str, ...] = Field(description="The full command line")
    environment: Mapping[str, str] = Field(
        description="The environment the process runs with"
    )


def _prepare(environment: SidecarEnvironment) -> _FilesAppUnderTest:
    app_name = AppName(f"files-{uuid4().hex[:8]}")
    dufs_port = free_port()
    instances_port = free_port()
    instances_url = f"http://{LOOPBACK_HOST}:{instances_port}"
    manifest_path = write_sidecar_manifest(
        environment.scratch_dir, app_name, instances_url
    )
    dufs_record_dir = install_fake_dufs(environment.scratch_dir / "fake-dufs")
    store_path = environment.scratch_dir / "apps" / "files" / "instances.json"
    return _FilesAppUnderTest(
        app_name=app_name,
        dufs_port=dufs_port,
        instances_port=instances_port,
        instances_url=instances_url,
        store_path=store_path,
        dufs_record_dir=dufs_record_dir,
        log_path=environment.scratch_dir / "files-app.log",
        command=(
            sys.executable,
            "-m",
            "files_app.main",
            "--manifest",
            str(manifest_path),
            "--app-url",
            f"http://localhost:{dufs_port}",
            "--instances-url",
            instances_url,
            "--store",
            str(store_path),
            "--dufs",
            str(environment.scratch_dir / "fake-dufs" / "bin" / "dufs"),
        ),
        environment={**os.environ, ENV_FAKE_DUFS_DIR: str(dufs_record_dir)},
    )


def _read_log(app: _FilesAppUnderTest) -> str:
    return app.log_path.read_text() if app.log_path.exists() else ""


def _spawn(app: _FilesAppUnderTest) -> subprocess.Popen[bytes]:
    # A session of its own puts the app and the fake dufs in one process group, so a failed
    # test can kill both rather than orphan the fake on its port.
    with app.log_path.open("wb") as log_file:
        return subprocess.Popen(
            app.command,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            env=app.environment,
            start_new_session=True,
        )


def _kill_if_running(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def _create(app: _FilesAppUnderTest, params: Mapping[str, str]) -> httpx.Response:
    return httpx.post(
        f"{app.instances_url}/_instances",
        json={"action": "new", "params": dict(params)},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )


def _listed_keys_and_urls(app: _FilesAppUnderTest) -> list[tuple[str, str]]:
    listed = httpx.get(
        f"{app.instances_url}/_instances", timeout=_REQUEST_TIMEOUT_SECONDS
    )
    assert listed.status_code == 200, listed.text
    return [
        (instance["key"], instance["url"]) for instance in listed.json()["instances"]
    ]


@pytest.mark.timeout(60)
def test_files_app_registers_runs_dufs_serves_instances_and_stops_with_dufs(
    files_environment: SidecarEnvironment,
) -> None:
    app = _prepare(files_environment)
    process = _spawn(app)
    try:
        assert wait_until(
            lambda: files_environment.registry_path.exists(),
            _STARTUP_TIMEOUT_SECONDS,
        ), _read_log(app)
        assert is_port_accepting(app.instances_port), _read_log(app)

        rows = read_registry(files_environment.registry_path)
        assert [(row.name, row.url, row.instances_url) for row in rows] == [
            (app.app_name, f"http://localhost:{app.dufs_port}", app.instances_url)
        ]

        # dufs runs as the sidecar's child with today's command line.
        assert wait_until(
            lambda: read_fake_dufs_argv(app.dufs_record_dir) is not None,
            _STARTUP_TIMEOUT_SECONDS,
        ), _read_log(app)
        assert read_fake_dufs_argv(app.dufs_record_dir) == [
            "--allow-all",
            "--bind",
            "127.0.0.1",
            "--port",
            str(app.dufs_port),
            "--assets",
            "system/apps/files/assets",
            "data",
        ]

        # The phase's manual check: an empty list, two creates, a delete that frees the number,
        # a create that reuses it, and a location report that lands in the store.
        assert _listed_keys_and_urls(app) == []
        first = _create(app, {"path": "/data/docs/"})
        assert first.status_code == 201, first.text
        assert first.json()["instance"] == {
            **first.json()["instance"],
            "key": "files-1",
            "url": "/data/docs/",
            "title": "File Viewer 1",
            "status": "idle",
            "lifetime": "referenced",
            "renameable": False,
        }
        second = _create(app, {})
        assert second.status_code == 201, second.text
        assert second.json()["instance"]["key"] == "files-2"
        assert second.json()["instance"]["url"] == "/"

        deleted = httpx.delete(
            f"{app.instances_url}/_instances/files-1",
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        assert deleted.status_code == 204
        reused = _create(app, {"path": "/data/notes/"})
        assert reused.status_code == 201, reused.text
        assert reused.json()["instance"]["key"] == "files-1"

        relocated = httpx.post(
            f"{app.instances_url}/_instances/files-2/location",
            json={"path": "/data/x/?q=readme"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        assert relocated.status_code == 200, relocated.text
        assert _listed_keys_and_urls(app) == [
            ("files-2", "/data/x/?q=readme"),
            ("files-1", "/data/notes/"),
        ]
        assert [
            instance["key"]
            for instance in json.loads(app.store_path.read_text())["instances"]
        ] == ["files-2", "files-1"]

        # Not renameable: the contract's files row.
        renamed = httpx.post(
            f"{app.instances_url}/_instances/files-2/rename",
            json={"title": "Docs"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        assert renamed.status_code == 400

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=_EXIT_TIMEOUT_SECONDS) == 143, _read_log(app)
        assert not is_port_accepting(app.instances_port)
    finally:
        _kill_if_running(process)
