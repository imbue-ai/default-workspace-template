"""Tests for the supervisord reader: the programs running now, over a fake control socket."""

import configparser
import xmlrpc.client
from pathlib import Path

from oom_priority import supervisor_rpc
from oom_priority.testing import fake_supervisord


def test_running_programs_are_read_from_the_supervisor_socket() -> None:
    with fake_supervisord(
        getAllProcessInfo=lambda: [
            {"name": "budget", "statename": "RUNNING", "pid": 40},
            {"name": "notes", "statename": "STOPPED", "pid": 0},
            {"name": "chat", "statename": "RUNNING", "pid": 25},
        ]
    ) as sock:
        assert supervisor_rpc.supervised_pids(sock) == {"budget": 40, "chat": 25}
        # No supervisord to ask: no programs, and no error.
        assert supervisor_rpc.supervised_pids(sock.with_name("missing.sock")) == {}


def test_the_supervisor_socket_is_the_one_the_workspace_configures() -> None:
    conf = configparser.ConfigParser(interpolation=None, strict=False)
    conf.read(Path(__file__).parents[4] / "supervisord.conf")

    assert conf["unix_http_server"]["file"] == str(supervisor_rpc.SUPERVISOR_SOCKET)


def test_a_program_is_stopped_through_the_supervisor_socket_without_waiting() -> None:
    asked: list[tuple[object, object]] = []

    def stop_process(name: object, wait: object) -> bool:
        asked.append((name, wait))
        if name == "notes":
            raise xmlrpc.client.Fault(70, "NOT_RUNNING: notes")
        if name == "ghost":
            raise xmlrpc.client.Fault(10, "BAD_NAME: ghost")
        return True

    with fake_supervisord(stopProcess=stop_process) as sock:
        assert supervisor_rpc.stop_program("budget", sock) is True
        # A program that is not running is already what a stop asks for; an unknown one is not stopped.
        assert supervisor_rpc.stop_program("notes", sock) is True
        assert supervisor_rpc.stop_program("ghost", sock) is False
        # No supervisord to ask: nothing stopped, and no error.
        assert (
            supervisor_rpc.stop_program("budget", sock.with_name("missing.sock"))
            is False
        )
    assert asked == [("budget", False), ("notes", False), ("ghost", False)]
