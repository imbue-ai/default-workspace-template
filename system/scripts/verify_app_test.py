"""Tests for verify_app.py."""

import http.server
import json
import socketserver
import threading
import time
import xmlrpc.client
from pathlib import Path

import pytest
import verify_app

_SCRIPT = Path(__file__).parent / "verify_app.py"


def test_resolve_target_from_port(tmp_path: Path) -> None:
    port, name, runner = verify_app._resolve_target("8085", tmp_path)
    assert port == 8085
    assert name is None
    assert runner is None


def test_resolve_target_from_url(tmp_path: Path) -> None:
    port, name, runner = verify_app._resolve_target(
        "http://127.0.0.1:9090/test", tmp_path
    )
    assert port == 9090
    assert name is None
    assert runner is None


def test_resolve_target_from_apps_toml(tmp_path: Path) -> None:
    state_dir = tmp_path / "data/.state"
    state_dir.mkdir(parents=True)
    (state_dir / "apps.toml").write_text(
        '[[apps]]\nname = "my-app"\nurl = "http://localhost:8123"\n'
    )
    port, name, runner = verify_app._resolve_target("my-app", tmp_path)
    assert port == 8123
    assert name == "my-app"


def test_resolve_target_from_supervisord_conf(tmp_path: Path) -> None:
    conf = tmp_path / "system/supervisord.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "[program:custom-app]\n"
        "command=python3 system/services/oom_priority/bin/oom_tag_service.py user "
        'bash -c "python3 system/scripts/forward_port.py --url http://localhost:8456 && custom-app"\n'
    )
    port, name, runner = verify_app._resolve_target("custom-app", tmp_path)
    assert port == 8456
    assert name == "custom-app"


class _MockHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"status": "ok", "pid": 1234, "started_at": time.time()}
                ).encode()
            )
        elif self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<!doctype html><html><body><h1>Welcome to My App</h1></body></html>"
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_poll_http_success_and_marker() -> None:
    server = socketserver.TCPServer(("127.0.0.1", 0), _MockHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}/"
        code, body = verify_app._poll_http(
            url, marker="Welcome to My App", timeout=2.0, interval=0.05
        )
        assert code == 200
        assert "Welcome to My App" in body
    finally:
        server.shutdown()
        server.server_close()


def test_poll_http_missing_marker_raises() -> None:
    server = socketserver.TCPServer(("127.0.0.1", 0), _MockHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}/"
        with pytest.raises(SystemExit) as exc_info:
            verify_app._poll_http(
                url, marker="NonExistentText", timeout=0.2, interval=0.05
            )
        assert "marker 'NonExistentText' was not found" in str(exc_info.value)
    finally:
        server.shutdown()
        server.server_close()


# --restart bounces a program through supervisord's RPC without waiting out its
# startsecs window and reads readiness from the port; these pin the state
# decisions that make that safe.
def test_stop_precedes_start_only_while_supervisord_counts_the_program_as_running() -> (
    None
):
    for state in ("RUNNING", "STARTING", "BACKOFF"):
        assert verify_app.needs_stop(state), state
    for state in ("STOPPED", "EXITED", "FATAL", "UNKNOWN"):
        assert not verify_app.needs_stop(state), state


def test_a_fresh_start_is_dead_in_every_state_but_starting_and_running() -> None:
    for state in ("BACKOFF", "FATAL", "EXITED", "STOPPED", "UNKNOWN"):
        assert verify_app.is_dead_after_start(state), state
    for state in ("STARTING", "RUNNING"):
        assert not verify_app.is_dead_after_start(state), state


def test_restart_raises_a_short_timeout_and_keeps_a_long_one() -> None:
    assert verify_app.effective_timeout(5.0, restart=False) == 5.0
    assert (
        verify_app.effective_timeout(5.0, restart=True)
        == verify_app.RESTART_MIN_TIMEOUT
    )
    assert verify_app.effective_timeout(60.0, restart=True) == 60.0


class _FakeSupervisor:
    """Records the RPC calls a restart makes and raises the faults it is given."""

    def __init__(
        self, state: str, stop_fault: int | None = None, start_fault: int | None = None
    ) -> None:
        self.state = state
        self.stop_fault = stop_fault
        self.start_fault = start_fault
        self.info_error: Exception | None = None
        self.stop_error: Exception | None = None
        self.calls: list[str] = []

    def getProcessInfo(self, name: str) -> dict[str, str]:  # noqa: N802 -- supervisord's RPC name
        if self.info_error is not None:
            raise self.info_error
        return {"statename": self.state}

    def stopProcess(self, name: str, wait: bool) -> bool:  # noqa: N802
        self.calls.append("stop")
        if self.stop_error is not None:
            raise self.stop_error
        if self.stop_fault is not None:
            raise xmlrpc.client.Fault(self.stop_fault, "fault")
        return True

    def startProcess(self, name: str, wait: bool) -> bool:  # noqa: N802
        self.calls.append("start")
        if self.start_fault is not None:
            raise xmlrpc.client.Fault(self.start_fault, "fault")
        self.state = "STARTING"
        return True


def test_restart_tolerates_a_program_that_exited_before_the_stop() -> None:
    sup = _FakeSupervisor("RUNNING", stop_fault=verify_app._FAULT_NOT_RUNNING)
    early_death = verify_app._restart_program(sup, "app")
    assert sup.calls == ["stop", "start"]
    assert early_death() is None


def test_restart_tolerates_autorestart_winning_the_start() -> None:
    sup = _FakeSupervisor("EXITED", start_fault=verify_app._FAULT_ALREADY_STARTED)
    verify_app._restart_program(sup, "app")
    assert sup.calls == ["start"]


def test_restart_fails_at_once_when_the_start_rpc_reports_a_crash() -> None:
    spawn_error = 50
    sup = _FakeSupervisor("STOPPED", start_fault=spawn_error)
    with pytest.raises(SystemExit, match="could not start app"):
        verify_app._restart_program(sup, "app")


def test_restart_fails_when_the_stop_rpc_reports_anything_but_not_running() -> None:
    sup = _FakeSupervisor("RUNNING", stop_fault=verify_app._FAULT_ALREADY_STARTED)
    with pytest.raises(SystemExit, match="could not stop app"):
        verify_app._restart_program(sup, "app")
    assert sup.calls == ["stop"]


def test_restart_fails_before_any_change_when_supervisord_is_unreachable() -> None:
    sup = _FakeSupervisor("RUNNING")
    sup.info_error = ConnectionRefusedError("refused")
    with pytest.raises(SystemExit, match="cannot reach supervisord"):
        verify_app._restart_program(sup, "app")
    assert sup.calls == []


def test_restart_fails_before_any_change_for_a_program_supervisord_does_not_know() -> (
    None
):
    bad_name = 10
    sup = _FakeSupervisor("RUNNING")
    sup.info_error = xmlrpc.client.Fault(bad_name, "BAD_NAME: app")
    with pytest.raises(
        SystemExit, match="supervisord knows no program 'app'.*supervisorctl update"
    ):
        verify_app._restart_program(sup, "app")
    assert sup.calls == []


def test_restart_reports_supervisord_going_away_during_the_stop() -> None:
    sup = _FakeSupervisor("RUNNING")
    sup.stop_error = ConnectionRefusedError("refused")
    with pytest.raises(SystemExit, match="lost supervisord .* while stopping app"):
        verify_app._restart_program(sup, "app")
    assert sup.calls == ["stop"]


def test_early_death_reports_a_program_that_crashed_after_the_start() -> None:
    sup = _FakeSupervisor("RUNNING")
    early_death = verify_app._restart_program(sup, "app")
    sup.state = "BACKOFF"
    failure = early_death()
    assert failure is not None
    assert "app is BACKOFF after the restart" in failure
    assert "app-stderr.log" in failure


def test_early_death_reports_supervisord_going_away() -> None:
    sup = _FakeSupervisor("RUNNING")
    early_death = verify_app._restart_program(sup, "app")
    sup.info_error = ConnectionRefusedError("refused")
    failure = early_death()
    assert failure is not None
    assert "lost track of app" in failure


def test_poll_http_ends_at_once_when_the_abort_check_reports_a_failure() -> None:
    with socketserver.TCPServer(("127.0.0.1", 0), _MockHandler) as server:
        port = server.server_address[1]
    started = time.monotonic()
    with pytest.raises(SystemExit, match="error: app died"):
        verify_app._poll_http(
            f"http://127.0.0.1:{port}/",
            marker=None,
            timeout=10.0,
            interval=0.05,
            abort_check=lambda: "app died",
        )
    assert time.monotonic() - started < 5.0


def test_supervisor_socket_path_honours_the_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(verify_app.SUPERVISOR_SOCKET_ENV, raising=False)
    assert verify_app.supervisor_socket_path() == verify_app.SUPERVISOR_SOCKET
    monkeypatch.setenv(verify_app.SUPERVISOR_SOCKET_ENV, "/tmp/other.sock")
    assert verify_app.supervisor_socket_path() == "/tmp/other.sock"
