"""Integration: ``terminal-app`` and ``terminal-pty`` as real processes around a fake ttyd and a fake tmux."""

import gzip
import json
import os
import signal
import subprocess
import sys
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
from app_manifest.manifest import MANIFEST_FILENAME
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
    pages_port: int = Field(description="The port the wrapper pages are served on")
    instances_port: int = Field(description="The port the instances API is served on")
    instances_url: str = Field(description="Where the instances API is served")
    paths: TerminalPaths = Field(description="The app's state directory layout")
    store_path: Path = Field(description="The instances.json the app is told to use")
    agent_state_dir: Path = Field(
        description="The fake agent state dir the discovery event lands in"
    )
    log_path: Path = Field(description="Where the app's stderr is captured")
    command: tuple[str, ...] = Field(description="The full command line")
    environment: Mapping[str, str] = Field(
        description="The environment the process runs with"
    )


class _TerminalPtyUnderTest(FrozenModel):
    """One terminal-pty process's command line, its port and files, and where its stderr lands."""

    app_name: AppName = Field(description="The unique name the pty registers")
    ttyd_port: int = Field(description="The port the fake ttyd is told to serve on")
    paths: TerminalPaths = Field(description="The state directory layout, shared with the app")
    ttyd_record_dir: Path = Field(description="Where the fake ttyd records its argv")
    log_path: Path = Field(description="Where the process's stderr is captured")
    command: tuple[str, ...] = Field(description="The full command line")
    environment: Mapping[str, str] = Field(
        description="The environment the process runs with"
    )


def _process_environment(environment: SidecarEnvironment, fake_tmux: FakeTmux) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{fake_tmux.bin_dir}{os.pathsep}{os.environ['PATH']}",
        ENV_FAKE_TMUX_DIR: str(fake_tmux.state_dir),
        "MNGR_AGENT_STATE_DIR": str(environment.scratch_dir / "agent-state"),
        "MNGR_PREFIX": "mngr-",
    }


def _prepare_app(
    environment: SidecarEnvironment, fake_tmux: FakeTmux
) -> _TerminalAppUnderTest:
    app_name = AppName(f"terminal-{uuid4().hex[:8]}")
    pages_port = free_port()
    instances_port = free_port()
    instances_url = f"http://{LOOPBACK_HOST}:{instances_port}"
    manifest_dir = environment.scratch_dir / "terminal"
    manifest_dir.mkdir()
    manifest_path = write_sidecar_manifest(manifest_dir, app_name, instances_url)
    store_path = environment.scratch_dir / "apps" / "terminal" / "instances.json"
    return _TerminalAppUnderTest(
        app_name=app_name,
        pages_port=pages_port,
        instances_port=instances_port,
        instances_url=instances_url,
        paths=TerminalPaths(state_dir=environment.scratch_dir / "state"),
        store_path=store_path,
        agent_state_dir=environment.scratch_dir / "agent-state",
        log_path=environment.scratch_dir / "terminal-app.log",
        command=(
            sys.executable,
            "-m",
            "terminal_app.main",
            "--manifest",
            str(manifest_path),
            "--app-url",
            f"http://localhost:{pages_port}",
            "--instances-url",
            instances_url,
            "--state-dir",
            str(environment.scratch_dir / "state"),
            "--store",
            str(store_path),
        ),
        environment=_process_environment(environment, fake_tmux),
    )


def _prepare_pty(
    environment: SidecarEnvironment, fake_tmux: FakeTmux
) -> _TerminalPtyUnderTest:
    app_name = AppName(f"terminal-pty-{uuid4().hex[:8]}")
    ttyd_port = free_port()
    manifest_dir = environment.scratch_dir / "terminal_pty"
    manifest_dir.mkdir()
    manifest_path = manifest_dir / MANIFEST_FILENAME
    manifest_path.write_text(
        f'name = "{app_name}"\ndisplay_name = "Terminal PTY"\ninternal = true\nprogram = "{app_name}"\n'
    )
    archive = environment.scratch_dir / "ttyd_index.html.gz"
    archive.write_bytes(gzip.compress(b"<html>patched</html>"))
    ttyd_record_dir = install_fake_ttyd(environment.scratch_dir / "fake-ttyd")
    return _TerminalPtyUnderTest(
        app_name=app_name,
        ttyd_port=ttyd_port,
        paths=TerminalPaths(state_dir=environment.scratch_dir / "state"),
        ttyd_record_dir=ttyd_record_dir,
        log_path=environment.scratch_dir / "terminal-pty.log",
        command=(
            sys.executable,
            "-m",
            "terminal_app.pty_main",
            "--manifest",
            str(manifest_path),
            "--app-url",
            f"http://localhost:{ttyd_port}",
            "--state-dir",
            str(environment.scratch_dir / "state"),
            "--ttyd-web-client",
            str(archive),
            "--ttyd",
            str(environment.scratch_dir / "fake-ttyd" / "bin" / "ttyd"),
        ),
        environment={**_process_environment(environment, fake_tmux), ENV_FAKE_TTYD_DIR: str(ttyd_record_dir)},
    )


def _read_log(log_path: Path) -> str:
    return log_path.read_text() if log_path.exists() else ""


def _spawn(command: tuple[str, ...], environment: Mapping[str, str], log_path: Path) -> subprocess.Popen[bytes]:
    # A session of its own puts the process and anything it starts in one process group, so a
    # failed test can kill both rather than orphan a fake on its port.
    with log_path.open("wb") as log_file:
        return subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            env=environment,
            start_new_session=True,
        )


def _kill_if_running(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


@pytest.mark.timeout(60)
def test_terminal_app_registers_serves_pages_and_sessions_and_stops_on_sigterm(
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
    app = _prepare_app(terminal_environment, fake_tmux)
    process = _spawn(app.command, app.environment, app.log_path)
    try:
        assert wait_until(
            lambda: terminal_environment.registry_path.exists(), _STARTUP_TIMEOUT_SECONDS
        ), _read_log(app.log_path)
        assert is_port_accepting(app.instances_port), _read_log(app.log_path)
        assert is_port_accepting(app.pages_port), _read_log(app.log_path)

        # The startup work: the discovery event and the registration.
        events = (
            (app.agent_state_dir / "events" / "servers" / "events.jsonl")
            .read_text()
            .splitlines()
        )
        assert [json.loads(line)["url"] for line in events] == [
            f"http://localhost:{app.pages_port}"
        ]
        rows = read_registry(terminal_environment.registry_path)
        assert [(row.name, row.url, row.instances_url) for row in rows] == [
            (app.app_name, f"http://localhost:{app.pages_port}", app.instances_url)
        ]

        # The instances API lists the user's sessions (not the agent's) and creates through the store.
        listed = httpx.get(f"{app.instances_url}/_instances", timeout=5.0)
        assert listed.status_code == 200
        assert [
            (instance["key"], instance["title"], instance["status"], instance["url"])
            for instance in listed.json()["instances"]
        ] == [("terminal-2", "Terminal 2", "idle", "/?session=terminal-2&tab={tab}")]
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
        assert [
            (session["name"], session["session_id"])
            for session in json.loads(app.store_path.read_text())["sessions"]
        ] == [("terminal-1", "$6")]

        # The wrapper pages: the session page frames the pty by the path the dispatch reads, with
        # the workdir the create recorded, and ``/new`` allocates and redirects.
        pages_url = f"http://{LOOPBACK_HOST}:{app.pages_port}"
        page = httpx.get(f"{pages_url}/?session=terminal-1&tab=tab-01", timeout=5.0)
        assert page.status_code == 200
        assert "<title>Terminal 1</title>" in page.text
        session = httpx.get(f"{pages_url}/api/sessions/terminal-1?tab=tab-01", timeout=5.0).json()
        assert session["pty_path"].startswith("/?arg=_&arg=session&arg=terminal-1&arg=tab-01&arg=")
        allocated = httpx.get(f"{pages_url}/new", timeout=5.0, follow_redirects=False)
        assert allocated.status_code == 302
        assert allocated.headers["location"] == "/?session=terminal-3"
        assert fake_tmux.session_names()[-1] == "terminal-3"

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
        assert process.wait(timeout=_EXIT_TIMEOUT_SECONDS) == 143, _read_log(app.log_path)
        assert not is_port_accepting(app.instances_port)
        assert not is_port_accepting(app.pages_port)
    finally:
        _kill_if_running(process)


@pytest.mark.timeout(60)
def test_terminal_pty_installs_dispatch_registers_and_becomes_ttyd(
    terminal_environment: SidecarEnvironment, tmp_path: Path
) -> None:
    fake_tmux = install_fake_tmux(tmp_path / "fake-tmux")
    pty = _prepare_pty(terminal_environment, fake_tmux)
    process = _spawn(pty.command, pty.environment, pty.log_path)
    try:
        assert wait_until(
            lambda: read_fake_ttyd_argv(pty.ttyd_record_dir) is not None, _STARTUP_TIMEOUT_SECONDS
        ), _read_log(pty.log_path)

        # The startup work: dispatch scripts, the patched web client, the registration.
        assert sorted(path.name for path in pty.paths.commands_dir.iterdir()) == [
            "agent.sh",
            "index.html",
            "session.sh",
            "workdir.sh",
        ]
        assert pty.paths.ttyd_index_path.read_bytes() == b"<html>patched</html>"
        rows = read_registry(terminal_environment.registry_path)
        assert [(row.name, row.url, row.internal) for row in rows] == [
            (pty.app_name, f"http://localhost:{pty.ttyd_port}", True)
        ]

        # ttyd replaced the process, the patched client on its command line.
        ttyd_argv = read_fake_ttyd_argv(pty.ttyd_record_dir)
        assert ttyd_argv is not None
        assert ttyd_argv[:8] == [
            "-p",
            str(pty.ttyd_port),
            "-a",
            "-t",
            "disableLeaveAlert=true",
            "-I",
            str(pty.paths.ttyd_index_path),
            "-W",
        ]
        assert f'SCRIPT="{pty.paths.commands_dir}/$KEY.sh"' in "\n".join(ttyd_argv)

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=_EXIT_TIMEOUT_SECONDS) == -signal.SIGTERM, _read_log(pty.log_path)
    finally:
        _kill_if_running(process)
