"""Tests for the app liveness probes and the supervisord stop/start actions.

Everything supervisord-shaped runs against ``FakeSupervisorServer`` (see
``testing.py``): a real XML-RPC server on a unix socket -- the same transport
supervisord's ``[unix_http_server]`` exposes -- so the custom unix-socket
transport is tested end to end rather than against a faked-out client.
"""

from pathlib import Path

import pytest

from imbue.system_interface.liveness import SupervisorProgramActionError
from imbue.system_interface.liveness import fetch_supervisor_program_states
from imbue.system_interface.liveness import probe_all_app_liveness
from imbue.system_interface.liveness import probe_tcp_url
from imbue.system_interface.liveness import start_supervisor_program
from imbue.system_interface.liveness import stop_supervisor_program
from imbue.system_interface.testing import FakeSupervisorServer


def test_fetch_supervisor_program_states_reports_a_starting_program_as_up(
    fake_supervisor: FakeSupervisorServer,
) -> None:
    fake_supervisor.statename_by_program["files"] = "STARTING"
    assert fetch_supervisor_program_states(fake_supervisor.socket_path) == {"files": True}


@pytest.mark.parametrize("statename", ["STOPPED", "STOPPING", "EXITED", "BACKOFF", "FATAL", "UNKNOWN"])
def test_fetch_supervisor_program_states_reports_a_down_program(
    fake_supervisor: FakeSupervisorServer, statename: str
) -> None:
    fake_supervisor.statename_by_program["files"] = statename
    assert fetch_supervisor_program_states(fake_supervisor.socket_path) == {"files": False}


def test_probe_tcp_url_reports_a_listening_backend(listening_port: int) -> None:
    assert probe_tcp_url(f"http://127.0.0.1:{listening_port}") is True


def test_probe_tcp_url_reports_a_closed_port(closed_port: int) -> None:
    assert probe_tcp_url(f"http://127.0.0.1:{closed_port}") is False


def test_probe_tcp_url_reports_an_unparseable_url_as_down() -> None:
    assert probe_tcp_url("not a url") is False


def test_fetch_supervisor_program_states_returns_every_program_in_one_call(
    fake_supervisor: FakeSupervisorServer,
) -> None:
    fake_supervisor.statename_by_program["files"] = "RUNNING"
    fake_supervisor.statename_by_program["terminal"] = "STARTING"
    fake_supervisor.statename_by_program["browser"] = "STOPPED"

    assert fetch_supervisor_program_states(fake_supervisor.socket_path) == {
        "files": True,
        "terminal": True,
        "browser": False,
    }


def test_fetch_supervisor_program_states_answers_none_without_a_socket(tmp_path: Path) -> None:
    assert fetch_supervisor_program_states(tmp_path / "absent.sock") is None


def test_probe_all_app_liveness_answers_supervised_rows_from_one_rpc_and_probes_the_rest(
    fake_supervisor: FakeSupervisorServer, listening_port: int, closed_port: int
) -> None:
    """Supervised rows read the batched supervisord answer (even while something
    still listens on the port); rows supervisord does not know, and unsupervised
    rows, fall back to their TCP probe."""
    fake_supervisor.statename_by_program["web"] = "STOPPED"
    fake_supervisor.statename_by_program["files"] = "RUNNING"

    is_running_by_name = probe_all_app_liveness(
        [
            ("web", "web", f"http://127.0.0.1:{listening_port}"),
            ("files", "files", f"http://127.0.0.1:{closed_port}"),
            ("forgotten", "no-such-program", f"http://127.0.0.1:{listening_port}"),
            ("plain", "", f"http://127.0.0.1:{closed_port}"),
        ]
    )

    assert is_running_by_name == {"web": False, "files": True, "forgotten": True, "plain": False}


def test_probe_all_app_liveness_falls_back_to_tcp_when_supervisord_is_unreachable(
    tmp_path: Path, listening_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINDS_SUPERVISOR_SOCKET", str(tmp_path / "absent.sock"))

    is_running_by_name = probe_all_app_liveness([("web", "web", f"http://127.0.0.1:{listening_port}")])

    assert is_running_by_name == {"web": True}


def test_probe_all_app_liveness_makes_no_rpc_when_no_row_is_supervised(
    fake_supervisor: FakeSupervisorServer, listening_port: int, closed_port: int
) -> None:
    """The sweep runs on a timer regardless of registry contents, so a registry
    with only unsupervised rows (or none) must not cost a supervisord round
    trip per pass -- every row is answered by its TCP probe alone."""
    is_running_by_name = probe_all_app_liveness(
        [
            ("plain", "", f"http://127.0.0.1:{listening_port}"),
            ("other", "", f"http://127.0.0.1:{closed_port}"),
        ]
    )

    assert is_running_by_name == {"plain": True, "other": False}
    assert probe_all_app_liveness([]) == {}
    assert fake_supervisor.get_all_process_info_call_count == 0


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
