"""The workspace's supervised programs and the pid each runs as, from supervisord's own XML-RPC socket.

Asked over the socket (``getAllProcessInfo``), as the shell's liveness sweep asks it, rather than by running
``supervisorctl``: the page re-reads every few seconds while shown, and each ``supervisorctl`` is a fresh Python
interpreter, slow to start under gVisor.
"""

import os
import socket
import xmlrpc.client
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from http.client import HTTPConnection
from pathlib import Path
from typing import Any
from typing import Final

from pydantic import Field
from pydantic import TypeAdapter
from pydantic import ValidationError

from activity.errors import SupervisorUnavailableError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# The socket supervisord's ``[unix_http_server]`` section binds (system/supervisord.conf); the shell reads the same
# override variable.
DEFAULT_SUPERVISOR_SOCKET_PATH: Final[str] = "/var/run/supervisor.sock"
ENV_SUPERVISOR_SOCKET: Final[str] = "MINDS_SUPERVISOR_SOCKET"
SUPERVISOR_TIMEOUT_SECONDS: Final[float] = 2.0

ReadProcessInfo = Callable[[], Sequence[Mapping[str, Any]]]
PROCESS_INFO_LIST: Final[TypeAdapter[list[dict[str, Any]]]] = TypeAdapter(list[dict[str, Any]])


class SupervisedProgram(FrozenModel):
    """One supervisord program and its state."""

    name: str = Field(description="The program name")
    state: str = Field(description="supervisord's state for it: RUNNING, STOPPED, EXITED, FATAL, ...")
    pid: int | None = Field(description="The pid it runs as, or None while it is not running")


def supervisor_socket_path() -> Path:
    return Path(os.environ.get(ENV_SUPERVISOR_SOCKET, DEFAULT_SUPERVISOR_SOCKET_PATH))


class _UnixSocketHttpConnection(HTTPConnection):
    """An HTTPConnection whose transport is a unix domain socket."""

    def __init__(self, socket_path: Path) -> None:
        super().__init__("localhost")
        self._socket_path = socket_path

    def connect(self) -> None:
        unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unix_socket.settimeout(SUPERVISOR_TIMEOUT_SECONDS)
        unix_socket.connect(str(self._socket_path))
        self.sock = unix_socket


class _UnixSocketTransport(xmlrpc.client.Transport):
    """An xmlrpc transport that dials a unix socket instead of a TCP host."""

    def __init__(self, socket_path: Path) -> None:
        super().__init__()
        self._socket_path = socket_path

    def make_connection(self, host: str | tuple[str, dict[str, str]]) -> HTTPConnection:
        return _UnixSocketHttpConnection(self._socket_path)


def socket_process_info_reader(socket_path: Path) -> ReadProcessInfo:
    """A reader that asks supervisord at ``socket_path`` for every program's process info."""

    def read() -> Sequence[Mapping[str, Any]]:
        proxy = xmlrpc.client.ServerProxy("http://localhost/RPC2", transport=_UnixSocketTransport(socket_path))
        try:
            return PROCESS_INFO_LIST.validate_python(proxy.supervisor.getAllProcessInfo())
        except ValidationError as e:
            raise SupervisorUnavailableError(f"getAllProcessInfo answered something other than a list: {e}") from e

    return read


@pure
def programs_from_process_info(process_info: Sequence[Mapping[str, Any]]) -> list[SupervisedProgram]:
    """``getAllProcessInfo`` entries as programs; supervisord reports pid 0 for a program that is not running."""
    return [
        SupervisedProgram(
            name=str(entry["name"]),
            state=str(entry["statename"]),
            pid=int(entry["pid"]) if int(entry.get("pid", 0)) > 0 else None,
        )
        for entry in process_info
    ]


def read_supervised_programs(read_process_info: ReadProcessInfo) -> list[SupervisedProgram]:
    try:
        process_info = read_process_info()
    except (OSError, xmlrpc.client.Error) as e:
        raise SupervisorUnavailableError(f"could not ask supervisord: {e}") from e
    try:
        return programs_from_process_info(process_info)
    except (KeyError, TypeError, ValueError) as e:
        raise SupervisorUnavailableError(f"supervisord answered in an unexpected shape: {e}") from e
