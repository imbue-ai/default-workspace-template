import xmlrpc.client
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from activity.errors import SupervisorUnavailableError
from activity.supervised_programs import programs_from_process_info
from activity.supervised_programs import read_supervised_programs
from activity.supervised_programs import socket_process_info_reader
from activity.testing import fake_supervisor_socket
from activity.testing import process_info


def test_a_running_program_carries_its_pid_and_a_stopped_one_none() -> None:
    programs = programs_from_process_info([process_info("chat", "RUNNING", 544), process_info("files", "STOPPED", 0)])
    assert [(program.name, program.state, program.pid) for program in programs] == [
        ("chat", "RUNNING", 544),
        ("files", "STOPPED", None),
    ]


def test_supervisord_unreachable_or_answering_oddly_is_an_error_not_an_empty_list() -> None:
    def refuse() -> Sequence[Mapping[str, Any]]:
        raise ConnectionRefusedError("no socket")

    def fault() -> Sequence[Mapping[str, Any]]:
        raise xmlrpc.client.Fault(1, "UNKNOWN_METHOD")

    with pytest.raises(SupervisorUnavailableError, match="could not ask supervisord: no socket"):
        read_supervised_programs(refuse)
    with pytest.raises(SupervisorUnavailableError, match="UNKNOWN_METHOD"):
        read_supervised_programs(fault)
    with pytest.raises(SupervisorUnavailableError, match="unexpected shape"):
        read_supervised_programs(lambda: [{"name": "chat"}])


def test_the_socket_reader_reports_a_missing_socket_as_unavailable(tmp_path: Path) -> None:
    with pytest.raises(SupervisorUnavailableError, match="could not ask supervisord"):
        read_supervised_programs(socket_process_info_reader(tmp_path / "no-such.sock"))


def test_the_socket_reader_asks_supervisord_over_its_unix_socket() -> None:
    with fake_supervisor_socket([process_info("chat", "RUNNING", 544)]) as socket_path:
        programs = read_supervised_programs(socket_process_info_reader(socket_path))
    assert [(program.name, program.pid) for program in programs] == [("chat", 544)]


def test_a_socket_answer_that_is_not_a_list_of_programs_is_unavailable() -> None:
    with fake_supervisor_socket("busy") as socket_path:
        with pytest.raises(SupervisorUnavailableError, match="getAllProcessInfo answered something other than a list"):
            read_supervised_programs(socket_process_info_reader(socket_path))
