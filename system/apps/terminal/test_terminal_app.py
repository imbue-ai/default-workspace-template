"""Integration: ``terminal-app`` as a real process around a fake ttyd and a fake tmux."""

import gzip
import json
import os
import signal
import subprocess
import sys
import urllib.parse
from collections.abc import Mapping
from datetime import datetime, timezone
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
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field
from terminal_app.data_types import TerminalPaths
from terminal_app.testing import (
    ENV_FAKE_TMUX_DIR,
    ENV_FAKE_TTYD_DIR,
    FakeTmux,
    expected_session_id_file,
    install_fake_tmux,
    install_fake_ttyd,
    make_tmux_session,
    read_fake_ttyd_argv,
)

_STARTUP_TIMEOUT_SECONDS: Final[float] = 20.0
_EXIT_TIMEOUT_SECONDS: Final[float] = 10.0


class _TerminalAppUnderTest(FrozenModel):
    """One terminal-app process's command line, its ports and files, and where its stderr lands."""

    app_name: AppName = Field(description="The unique name the app registers")
    ttyd_port: int = Field(description="The port the fake ttyd is told to serve on")
    instances_port: int = Field(description="The port the instances API is served on")
    instances_url: str = Field(description="Where the instances API is served")
    paths: TerminalPaths = Field(description="The app's state directory layout")
    store_path: Path = Field(description="The instances.json the app is told to use")
    agent_state_dir: Path = Field(
        description="The fake agent state dir the discovery event lands in"
    )
    ttyd_record_dir: Path = Field(description="Where the fake ttyd records its argv")
    log_path: Path = Field(description="Where the app's stderr is captured")
    command: tuple[str, ...] = Field(description="The full command line")
    environment: Mapping[str, str] = Field(
        description="The environment the process runs with"
    )


def _prepare(
    environment: SidecarEnvironment, fake_tmux: FakeTmux, is_registered: bool = True
) -> _TerminalAppUnderTest:
    app_name = AppName(f"terminal-{uuid4().hex[:8]}")
    ttyd_port = free_port()
    instances_port = free_port()
    instances_url = f"http://{LOOPBACK_HOST}:{instances_port}"
    manifest_path = write_sidecar_manifest(
        environment.scratch_dir, app_name, instances_url
    )
    archive = environment.scratch_dir / "ttyd_index.html.gz"
    archive.write_bytes(gzip.compress(b"<html>patched</html>"))
    ttyd_record_dir = install_fake_ttyd(environment.scratch_dir / "fake-ttyd")
    store_path = environment.scratch_dir / "apps" / "terminal" / "instances.json"
    agent_state_dir = environment.scratch_dir / "agent-state"
    process_environment = {
        **os.environ,
        "PATH": f"{fake_tmux.bin_dir}{os.pathsep}{os.environ['PATH']}",
        ENV_FAKE_TMUX_DIR: str(fake_tmux.state_dir),
        ENV_FAKE_TTYD_DIR: str(ttyd_record_dir),
        "MNGR_AGENT_STATE_DIR": str(agent_state_dir),
        "MNGR_PREFIX": "mngr-",
    }
    return _TerminalAppUnderTest(
        app_name=app_name,
        ttyd_port=ttyd_port,
        instances_port=instances_port,
        instances_url=instances_url,
        paths=TerminalPaths(state_dir=environment.scratch_dir / "state"),
        store_path=store_path,
        agent_state_dir=agent_state_dir,
        ttyd_record_dir=ttyd_record_dir,
        log_path=environment.scratch_dir / "terminal-app.log",
        command=(
            sys.executable,
            "-m",
            "terminal_app.main",
            "--manifest",
            str(manifest_path),
            "--app-url",
            f"http://localhost:{ttyd_port}",
            "--instances-url",
            instances_url,
            "--state-dir",
            str(environment.scratch_dir / "state"),
            "--store",
            str(store_path),
            "--ttyd-web-client",
            str(archive),
            "--ttyd",
            str(environment.scratch_dir / "fake-ttyd" / "bin" / "ttyd"),
            *(() if is_registered else ("--no-register",)),
        ),
        environment=process_environment,
    )


def _read_log(app: _TerminalAppUnderTest) -> str:
    return app.log_path.read_text() if app.log_path.exists() else ""


def _spawn(app: _TerminalAppUnderTest) -> subprocess.Popen[bytes]:
    # A session of its own puts the app and the fake ttyd in one process group, so a failed
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


@pytest.mark.timeout(60)
def test_terminal_app_installs_dispatch_registers_serves_sessions_and_stops_with_ttyd(
    terminal_environment: SidecarEnvironment, tmp_path: Path
) -> None:
    fake_tmux = install_fake_tmux(tmp_path / "fake-tmux")
    fake_tmux.set_sessions(
        [
            make_tmux_session("terminal-2", "$5", datetime(2026, 9, 3, tzinfo=timezone.utc)),
            make_tmux_session("mngr-alice", "$1"),
        ]
    )
    fake_tmux.set_clients([])
    app = _prepare(terminal_environment, fake_tmux)
    process = _spawn(app)
    try:
        assert wait_until(
            lambda: (
                app.paths.commands_dir.exists()
                and terminal_environment.registry_path.exists()
            ),
            _STARTUP_TIMEOUT_SECONDS,
        ), _read_log(app)
        assert is_port_accepting(app.instances_port), _read_log(app)

        # The startup work: dispatch scripts, the patched web client, the discovery event.
        assert sorted(path.name for path in app.paths.commands_dir.iterdir()) == [
            "agent.sh",
            "index.html",
            "session.sh",
            "workdir.sh",
        ]
        assert app.paths.ttyd_index_path.read_bytes() == b"<html>patched</html>"
        events = (
            (app.agent_state_dir / "events" / "servers" / "events.jsonl")
            .read_text()
            .splitlines()
        )
        assert [json.loads(line)["url"] for line in events] == [
            f"http://localhost:{app.ttyd_port}"
        ]

        rows = read_registry(terminal_environment.registry_path)
        assert [(row.name, row.url, row.instances_url) for row in rows] == [
            (app.app_name, f"http://localhost:{app.ttyd_port}", app.instances_url)
        ]

        # ttyd runs as the sidecar's child, the patched client on its command line.
        assert wait_until(
            lambda: read_fake_ttyd_argv(app.ttyd_record_dir) is not None,
            _STARTUP_TIMEOUT_SECONDS,
        ), _read_log(app)
        ttyd_argv = read_fake_ttyd_argv(app.ttyd_record_dir)
        assert ttyd_argv is not None
        assert ttyd_argv[:8] == [
            "-p",
            str(app.ttyd_port),
            "-a",
            "-t",
            "disableLeaveAlert=true",
            "-I",
            str(app.paths.ttyd_index_path),
            "-W",
        ]
        assert f'SCRIPT="{app.paths.commands_dir}/$KEY.sh"' in "\n".join(ttyd_argv)

        # The instances API lists the user's sessions (not the agent's) and creates through the store.
        listed = httpx.get(f"{app.instances_url}/_instances", timeout=5.0)
        assert listed.status_code == 200
        assert [
            (instance["key"], instance["title"], instance["status"])
            for instance in listed.json()["instances"]
        ] == [("terminal-2", "Terminal 2", "idle")]
        created = httpx.post(
            f"{app.instances_url}/_instances",
            json={"action": "new", "params": {}},
            timeout=5.0,
        )
        assert created.status_code == 201, created.text
        assert created.json()["instance"]["key"] == "terminal-1"
        assert created.json()["instance"]["status"] == "idle"
        # The session exists from the create, running the tagged login shell in the workdir.
        assert [session.name for session in fake_tmux.sessions()] == [
            "terminal-2",
            "mngr-alice",
            "terminal-1",
        ]
        create_call = fake_tmux.creates()[0]
        assert create_call[:6] == ["new-session", "-d", "-s", "terminal-1", "-c", os.getcwd()]
        assert create_call[-5:] == [
            "python3",
            str(Path("system/services/oom_priority/bin/oom_tag_service.py").absolute()),
            "terminal-session",
            "bash",
            "-l",
        ]
        assert (app.paths.sessions_dir / "terminal-1").read_text() == expected_session_id_file("$6")
        # A create naming no workdir starts the shell where the app runs: the cwd it was
        # spawned with, which is this test's.
        default_directory = urllib.parse.quote(os.getcwd(), safe="")
        assert (
            created.json()["instance"]["url"]
            == f"/?arg=_&arg=session&arg=terminal-1&arg={{tab}}&arg={default_directory}"
        )
        assert [
            (session["name"], session["session_id"])
            for session in json.loads(app.store_path.read_text())["sessions"]
        ] == [("terminal-1", "$6")]

        # The hook route is served by the same process.
        hook = httpx.post(
            f"{app.instances_url}/tmux-hook",
            json={
                "kind": "session-renamed",
                "client_tty": "",
                "session_name": "terminal-2",
                "session_id": "$5",
            },
            timeout=5.0,
        )
        assert hook.status_code == 204

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=_EXIT_TIMEOUT_SECONDS) == 143, _read_log(app)
        assert not is_port_accepting(app.instances_port)
    finally:
        _kill_if_running(process)


@pytest.mark.timeout(60)
def test_a_terminal_booted_without_registration_serves_but_registers_nothing(
    terminal_environment: SidecarEnvironment, tmp_path: Path
) -> None:
    # A preview boots the terminal on two free ports beside the live one; it must serve its
    # instances API and ttyd without touching the live terminal's registry row.
    fake_tmux = install_fake_tmux(tmp_path / "fake-tmux")
    fake_tmux.set_sessions([])
    fake_tmux.set_clients([])
    app = _prepare(terminal_environment, fake_tmux, is_registered=False)
    process = _spawn(app)
    try:
        assert wait_until(lambda: is_port_accepting(app.instances_port), _STARTUP_TIMEOUT_SECONDS), _read_log(app)
        assert wait_until(
            lambda: read_fake_ttyd_argv(app.ttyd_record_dir) is not None, _STARTUP_TIMEOUT_SECONDS
        ), _read_log(app)
        listed = httpx.get(f"{app.instances_url}/_instances", timeout=5.0)
        assert listed.status_code == 200
        assert not terminal_environment.registry_path.exists()
    finally:
        _kill_if_running(process)
