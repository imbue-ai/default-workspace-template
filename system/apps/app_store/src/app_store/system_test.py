import os
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from app_manifest.registry import FORWARD_PORT_SCRIPT
from app_store.catalog import NodeRecipe
from app_store.catalog import PythonRecipe
from app_store.errors import InstallError
from app_store.system import AnyRecipe
from app_store.system import CommandPackageInstaller
from app_store.system import ForwardPortRegistrar
from app_store.system import HttpPortProbe
from app_store.system import SubprocessCommandRunner
from app_store.system import SupervisorctlSupervisor
from app_store.system import install_steps
from app_store.testing import FakeCommandRunner


def _argvs(recipe: AnyRecipe, env: Path) -> list[tuple[str, ...]]:
    return [step.argv for step in install_steps(recipe, env)]


def test_a_python_recipe_makes_a_venv_then_installs_its_pin() -> None:
    env = Path("/apps/x/env")
    assert _argvs(PythonRecipe(kind="python", requirement="x==1.0"), env) == [
        ("uv", "venv", "--quiet", "--python", "3.12", "/apps/x/env"),
        ("uv", "pip", "install", "--python", "/apps/x/env/bin/python", "x==1.0"),
    ]


def test_a_node_recipe_installs_its_pin_into_its_own_prefix() -> None:
    assert _argvs(NodeRecipe(kind="node", package="x@1.0.0"), Path("/apps/x/env")) == [
        ("npm", "install", "--prefix", "/apps/x/env", "--no-audit", "--no-fund", "x@1.0.0")
    ]


def test_the_installer_stops_at_the_first_failing_command(tmp_path: Path) -> None:
    runner = FakeCommandRunner(exit_status=1)
    installer = CommandPackageInstaller(runner=runner)
    with pytest.raises(InstallError, match="x==1.0"):
        installer.install(PythonRecipe(kind="python", requirement="x==1.0"), tmp_path / "env", tmp_path / "log")
    assert len(runner.commands) == 1


def test_the_subprocess_runner_logs_output_and_exit_status(tmp_path: Path) -> None:
    log = tmp_path / "install.log"
    runner = SubprocessCommandRunner()
    assert runner.run(["python3", "-c", "print('hello')"], log, timeout_seconds=30) == 0
    assert runner.run(["python3", "-c", "import sys; sys.exit(3)"], log, timeout_seconds=30) == 3
    assert runner.run(["/no/such/program"], log, timeout_seconds=30) == -1
    assert runner.run(["sleep", "5"], log, timeout_seconds=0.2) == -1
    text = log.read_text()
    assert "hello" in text
    assert "exit status 3" in text
    assert "could not run /no/such/program" in text
    assert "gave up after" in text


def _fake_program(bin_dir: Path, name: str) -> Path:
    """A stand-in for ``name`` on PATH: it records its arguments, prints $FAKE_OUTPUT and exits $FAKE_STATUS."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    calls = bin_dir / f"{name}.calls"
    program = bin_dir / name
    program.write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\nprintf "%s" "$FAKE_OUTPUT"\nexit "${{FAKE_STATUS:-0}}"\n')
    program.chmod(0o755)
    return calls


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return bin_dir


def test_a_program_change_rereads_then_updates_only_that_program(fake_bin: Path) -> None:
    calls = _fake_program(fake_bin, "supervisorctl")
    SupervisorctlSupervisor().apply_program_change("datasette")
    assert calls.read_text().splitlines() == ["reread", "update datasette"]


@pytest.mark.parametrize(
    ("status", "output"), [("1", "busy"), ("0", "ERROR (no such group)")], ids=["exit-status", "error-in-output"]
)
def test_supervisord_refusing_a_change_is_an_install_error_with_its_answer(
    fake_bin: Path, monkeypatch: pytest.MonkeyPatch, status: str, output: str
) -> None:
    _fake_program(fake_bin, "supervisorctl")
    monkeypatch.setenv("FAKE_STATUS", status)
    monkeypatch.setenv("FAKE_OUTPUT", output)
    with pytest.raises(InstallError, match=f"supervisord refused reread: {re.escape(output)}"):
        SupervisorctlSupervisor().apply_program_change("datasette")


def test_without_supervisorctl_a_change_is_refused_and_a_stop_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(InstallError, match="supervisord refused reread"):
        SupervisorctlSupervisor().apply_program_change("datasette")
    SupervisorctlSupervisor().stop("datasette")


def test_stopping_a_program_supervisord_does_not_know_is_not_an_error(
    fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_program(fake_bin, "supervisorctl")
    monkeypatch.setenv("FAKE_STATUS", "1")
    monkeypatch.setenv("FAKE_OUTPUT", "datasette: ERROR (no such process)")
    SupervisorctlSupervisor().stop("datasette")
    monkeypatch.setenv("FAKE_OUTPUT", "datasette: ERROR (spawn error)")
    SupervisorctlSupervisor().stop("datasette")
    assert calls.read_text().splitlines() == ["stop datasette", "stop datasette"]


def test_unregistering_removes_the_row_through_the_forward_port_script(
    fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_program(fake_bin, "python3")
    ForwardPortRegistrar().unregister("datasette")
    monkeypatch.setenv("FAKE_STATUS", "1")
    ForwardPortRegistrar().unregister("marimo")
    assert calls.read_text().splitlines() == [
        f"{FORWARD_PORT_SCRIPT} --name datasette --remove",
        f"{FORWARD_PORT_SCRIPT} --name marimo --remove",
    ]


class _AnswerWith(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self) -> None:
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.mark.parametrize(("status", "is_answering"), [(200, True), (404, True), (502, False)])
def test_the_probe_counts_any_answer_below_500(status: int, is_answering: bool) -> None:
    handler = type("Handler", (_AnswerWith,), {"status": status})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert HttpPortProbe().wait_until_answering(server.server_address[1], timeout_seconds=0.6) is is_answering
    finally:
        server.shutdown()
        server.server_close()


def test_the_probe_gives_up_on_a_port_nothing_listens_on() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    assert HttpPortProbe().wait_until_answering(port, timeout_seconds=0.3) is False
