"""The app lifecycle end to end, over the shell's real threads: a running shell, a supervisord-shaped RPC server,
and a registry naming a stoppable app on a free loopback port. What a person sees is what is asserted: the app
stops a grace period after its last window closes once someone has visited, its port then answers a request with
the loading page and the app is started, and Quit closes the windows and stops the app at once."""

import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.server import create_application
from imbue.system_interface.shell.identity import RequestIdentity
from imbue.system_interface.shell.liveness import probe_all_app_liveness
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.testing import FakeSupervisorServer
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.testing import find_free_port
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_GRACE_SECONDS = 0.3
_SWEEP_SECONDS = 0.1


@pytest.fixture
def docs_port() -> int:
    return find_free_port()


@pytest.fixture
def started_shell(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, fake_supervisor: FakeSupervisorServer, docs_port: int
) -> Iterator[Flask]:
    """A started shell over a ``docs`` app that stops when no window shows it, running (as supervisord tells it)
    on ``docs_port`` where nothing actually listens, and a critical ``shell`` row."""
    fake_supervisor.statename_by_program["docs"] = "RUNNING"
    fake_supervisor.statename_by_program["shell"] = "RUNNING"
    registry_path = write_registry(
        tmp_path / "apps.toml",
        registry_row_toml("docs", f"http://127.0.0.1:{docs_port}", program="docs", stop_when_no_windows=True),
        registry_row_toml("shell", "http://127.0.0.1:1", program="shell", is_critical=True),
    )
    state = build_test_state(
        broadcaster=broadcaster,
        shell_state_directory=tmp_path / "state",
        inventory=build_inventory(registry_path, broadcaster, prober=probe_all_app_liveness),
        repo_root=tmp_path / "repo",
        static_directory=tmp_path / "static",
        is_lifecycle_enabled=True,
        no_windows_grace_seconds=_GRACE_SECONDS,
        idle_sweep_interval_seconds=_SWEEP_SECONDS,
    )
    application = create_application(state)
    state.shell.start()
    try:
        yield application
    finally:
        state.shell.stop()


def _fetch(port: int) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/some/page", timeout=5.0) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _windows(client: FlaskClient) -> list[list[str]]:
    listing = client.get("/api/desktops").get_json()
    return [[window["app"] for window in desktop["windows"]] for desktop in listing["desktops"]]


def _wait_for_sweeps(fake_supervisor: FakeSupervisorServer, count: int) -> None:
    """Wait until the lifecycle sweep has read supervisord ``count`` more times, so a "nothing happened" assertion
    covers passes that actually ran."""
    target = fake_supervisor.get_all_process_info_call_count + count
    wait_for(
        lambda: fake_supervisor.get_all_process_info_call_count >= target,
        timeout=5.0,
        poll_interval=0.02,
        error_message="the lifecycle sweep stopped running",
    )


def test_an_app_with_no_windows_stops_after_the_grace_and_comes_back_on_a_request(
    started_shell: Flask, fake_supervisor: FakeSupervisorServer, docs_port: int
) -> None:
    client = started_shell.test_client()
    # Nobody has visited: the app keeps running well past the grace period.
    _wait_for_sweeps(fake_supervisor, 5)
    assert fake_supervisor.statename_by_program["docs"] == "RUNNING"

    # A visit, then a window opened and closed: the app stops a grace period after the close and its port is parked.
    shell = started_shell.config["SYSTEM_INTERFACE_STATE"].shell
    shell.arrive_client(ClientId("laptop"), RequestIdentity(owner=True))
    opened = client.post("/api/desktops/home/windows", json={"app": "docs", "path": "/a/", "client_id": "laptop"})
    assert opened.status_code == 201
    window_id = opened.get_json()["window"]["id"]
    assert client.post(f"/api/desktops/home/windows/{window_id}/close").status_code == 204
    wait_for(
        lambda: fake_supervisor.statename_by_program["docs"] == "STOPPED",
        timeout=5.0,
        poll_interval=0.05,
        error_message="the app was not stopped after its last window closed",
    )
    wait_for(lambda: shell.lifecycle.is_app_parked("docs"), timeout=5.0, poll_interval=0.05)
    assert client.get("/api/inventory").get_json()["apps"][0]["is_running"] is False

    # The next request for the app is answered with the loading page, and the app is started.
    status, body = _fetch(docs_port)
    assert status == 503 and "Starting Docs" in body
    wait_for(
        lambda: fake_supervisor.statename_by_program["docs"] == "RUNNING",
        timeout=5.0,
        poll_interval=0.05,
        error_message="the request did not start the app",
    )
    wait_for(lambda: not shell.lifecycle.is_app_parked("docs"), timeout=5.0, poll_interval=0.05)


def test_quit_closes_the_windows_and_stops_the_app_at_once(
    started_shell: Flask, fake_supervisor: FakeSupervisorServer, docs_port: int
) -> None:
    client = started_shell.test_client()
    shell = started_shell.config["SYSTEM_INTERFACE_STATE"].shell
    shell.arrive_client(ClientId("laptop"), RequestIdentity(owner=True))
    assert client.post("/api/desktops/home/windows", json={"app": "docs", "path": "/a/", "client_id": "laptop"}).status_code == 201
    assert client.post("/api/desktops/home/windows", json={"app": "docs", "path": "/b/", "client_id": "laptop"}).status_code == 201
    assert _windows(client) == [["docs", "docs"]]

    answer = client.post("/api/apps/docs/quit")

    assert answer.status_code == 200 and answer.get_json() == {"name": "docs", "is_running": False}
    assert _windows(client) == [[]]
    assert fake_supervisor.statename_by_program["docs"] == "STOPPED"
    wait_for(lambda: shell.lifecycle.is_app_parked("docs"), timeout=5.0, poll_interval=0.05)
    # The critical app is never parked, whatever supervisord says of it.
    fake_supervisor.statename_by_program["shell"] = "STOPPED"
    _wait_for_sweeps(fake_supervisor, 3)
    assert shell.lifecycle.parked_app_names() == ["docs"]
