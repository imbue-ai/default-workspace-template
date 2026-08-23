"""Tests for the app liveness probes and the supervisord stop/start actions.

Everything supervisord-shaped runs against ``FakeSupervisorServer`` (see
``testing.py``): a real XML-RPC server on a unix socket -- the same transport
supervisord's ``[unix_http_server]`` exposes -- so the custom unix-socket
transport is tested end to end rather than against a faked-out client.
"""

from pathlib import Path

import pytest

from imbue.system_interface.liveness import SupervisorProgramActionError
from imbue.system_interface.liveness import probe_app_liveness
from imbue.system_interface.liveness import probe_supervisor_program
from imbue.system_interface.liveness import probe_tcp_url
from imbue.system_interface.liveness import start_supervisor_program
from imbue.system_interface.liveness import stop_supervisor_program
from imbue.system_interface.testing import FakeSupervisorServer


def test_probe_supervisor_program_reports_a_running_program(fake_supervisor: FakeSupervisorServer) -> None:
    fake_supervisor.statename_by_program["files"] = "RUNNING"
    assert probe_supervisor_program("files", fake_supervisor.socket_path) is True


def test_probe_supervisor_program_reports_a_starting_program_as_up(
    fake_supervisor: FakeSupervisorServer,
) -> None:
    fake_supervisor.statename_by_program["files"] = "STARTING"
    assert probe_supervisor_program("files", fake_supervisor.socket_path) is True


@pytest.mark.parametrize("statename", ["STOPPED", "STOPPING", "EXITED", "BACKOFF", "FATAL", "UNKNOWN"])
def test_probe_supervisor_program_reports_a_down_program(
    fake_supervisor: FakeSupervisorServer, statename: str
) -> None:
    fake_supervisor.statename_by_program["files"] = statename
    assert probe_supervisor_program("files", fake_supervisor.socket_path) is False


def test_probe_supervisor_program_answers_none_for_an_unknown_program(
    fake_supervisor: FakeSupervisorServer,
) -> None:
    """A program supervisord does not know (hand-edited registry, removed
    block) is 'cannot say', not 'stopped' -- the caller falls back to TCP."""
    assert probe_supervisor_program("no-such-program", fake_supervisor.socket_path) is None


def test_probe_supervisor_program_answers_none_without_a_socket(tmp_path: Path) -> None:
    assert probe_supervisor_program("files", tmp_path / "absent.sock") is None


def test_probe_tcp_url_reports_a_listening_backend(listening_port: int) -> None:
    assert probe_tcp_url(f"http://127.0.0.1:{listening_port}") is True


def test_probe_tcp_url_reports_a_closed_port(closed_port: int) -> None:
    assert probe_tcp_url(f"http://127.0.0.1:{closed_port}") is False


def test_probe_tcp_url_reports_an_unparseable_url_as_down() -> None:
    assert probe_tcp_url("not a url") is False


def test_probe_app_liveness_prefers_the_supervisor_answer(
    fake_supervisor: FakeSupervisorServer, listening_port: int
) -> None:
    """A supervised row reads supervisord's state even while something still
    answers on the port (a program mid-STOPPING keeps its socket briefly)."""
    fake_supervisor.statename_by_program["web"] = "STOPPED"
    assert probe_app_liveness("web", f"http://127.0.0.1:{listening_port}") is False


def test_probe_app_liveness_falls_back_to_tcp_when_supervisord_cannot_say(
    tmp_path: Path, listening_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINDS_SUPERVISOR_SOCKET", str(tmp_path / "absent.sock"))
    assert probe_app_liveness("web", f"http://127.0.0.1:{listening_port}") is True


def test_probe_app_liveness_probes_tcp_for_an_unsupervised_row(listening_port: int, closed_port: int) -> None:
    assert probe_app_liveness("", f"http://127.0.0.1:{listening_port}") is True
    assert probe_app_liveness("", f"http://127.0.0.1:{closed_port}") is False


def test_stop_supervisor_program_stops_a_running_program(fake_supervisor: FakeSupervisorServer) -> None:
    fake_supervisor.statename_by_program["docs"] = "RUNNING"
    stop_supervisor_program("docs", fake_supervisor.socket_path)
    assert fake_supervisor.statename_by_program["docs"] == "STOPPED"


def test_stop_supervisor_program_is_idempotent_on_a_stopped_program(
    fake_supervisor: FakeSupervisorServer,
) -> None:
    fake_supervisor.statename_by_program["docs"] = "STOPPED"
    stop_supervisor_program("docs", fake_supervisor.socket_path)
    assert fake_supervisor.statename_by_program["docs"] == "STOPPED"


def test_start_supervisor_program_starts_a_stopped_program(fake_supervisor: FakeSupervisorServer) -> None:
    fake_supervisor.statename_by_program["docs"] = "STOPPED"
    start_supervisor_program("docs", fake_supervisor.socket_path)
    assert fake_supervisor.statename_by_program["docs"] == "RUNNING"


def test_start_supervisor_program_is_idempotent_on_a_running_program(
    fake_supervisor: FakeSupervisorServer,
) -> None:
    fake_supervisor.statename_by_program["docs"] = "RUNNING"
    start_supervisor_program("docs", fake_supervisor.socket_path)
    assert fake_supervisor.statename_by_program["docs"] == "RUNNING"


def test_actions_raise_on_an_unknown_program(fake_supervisor: FakeSupervisorServer) -> None:
    with pytest.raises(SupervisorProgramActionError, match="BAD_NAME"):
        start_supervisor_program("no-such-program", fake_supervisor.socket_path)
    with pytest.raises(SupervisorProgramActionError, match="BAD_NAME"):
        stop_supervisor_program("no-such-program", fake_supervisor.socket_path)


def test_actions_raise_when_supervisord_is_unreachable(tmp_path: Path) -> None:
    with pytest.raises(SupervisorProgramActionError, match="could not reach"):
        start_supervisor_program("docs", tmp_path / "absent.sock")
    with pytest.raises(SupervisorProgramActionError, match="could not reach"):
        stop_supervisor_program("docs", tmp_path / "absent.sock")
