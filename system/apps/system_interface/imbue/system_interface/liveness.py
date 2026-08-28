"""Liveness probes behind ``AppEntry.is_running``.

The registry row is an app's identity; whether it is running is derived state,
never stored. A row carrying a ``program`` (see ``forward_port.py --program``)
is supervised, so supervisord's own process state -- read over the same RPC
socket ``supervisorctl`` uses -- is the authority. A row without one is managed
outside the workspace, so the best available signal is whether anything answers
a TCP connect on the row's registered URL.
"""

import os
import socket
import urllib.parse
import xmlrpc.client
from collections.abc import Sequence
from http.client import HTTPConnection
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger

# The socket supervisord's ``[unix_http_server]`` section binds (see
# ``system/supervisord.conf``); override for tests via the env var.
DEFAULT_SUPERVISOR_SOCKET_PATH: Final[str] = "/var/run/supervisor.sock"
ENV_SUPERVISOR_SOCKET: Final[str] = "MINDS_SUPERVISOR_SOCKET"

# One probe (RPC call or TCP connect) must never stall the liveness sweep: both
# targets are loopback-local, so anything slower than this is effectively down.
_PROBE_TIMEOUT_SECONDS: Final[float] = 2.0

# The supervisord process states that mean "the program is up (or coming up)".
# STOPPED / STOPPING / EXITED / BACKOFF / FATAL / UNKNOWN all render as stopped.
_RUNNING_STATE_NAMES: Final[frozenset[str]] = frozenset({"RUNNING", "STARTING"})


def supervisor_socket_path() -> Path:
    return Path(os.environ.get(ENV_SUPERVISOR_SOCKET, DEFAULT_SUPERVISOR_SOCKET_PATH))


class _UnixSocketHttpConnection(HTTPConnection):
    """An HTTPConnection whose transport is a unix domain socket."""

    def __init__(self, socket_path: Path) -> None:
        super().__init__("localhost")
        self._socket_path = socket_path

    def connect(self) -> None:
        unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unix_socket.settimeout(_PROBE_TIMEOUT_SECONDS)
        unix_socket.connect(str(self._socket_path))
        self.sock = unix_socket


class _UnixSocketTransport(xmlrpc.client.Transport):
    """An xmlrpc transport that dials a unix socket instead of a TCP host."""

    def __init__(self, socket_path: Path) -> None:
        super().__init__()
        self._socket_path = socket_path

    def make_connection(self, host: str | tuple[str, dict[str, str]]) -> HTTPConnection:
        return _UnixSocketHttpConnection(self._socket_path)


class SupervisorProgramActionError(RuntimeError):
    """Raised when supervisord refuses, or cannot be reached for, a stop/start."""

    ...


# Fault codes from supervisord's ``supervisor.xmlrpc.Faults`` -- pinned here
# because the supervisor package is not a dependency of this app; the RPC
# protocol is the contract.
_FAULT_ALREADY_STARTED: Final[int] = 60
_FAULT_NOT_RUNNING: Final[int] = 70


def _supervisor_proxy(socket_path: Path) -> xmlrpc.client.ServerProxy:
    return xmlrpc.client.ServerProxy("http://localhost/RPC2", transport=_UnixSocketTransport(socket_path))


def start_supervisor_program(program: str, socket_path: Path) -> None:
    """Ask supervisord to start ``program``. Idempotent: already-started is success.

    ``wait=False`` so the RPC answers immediately and the liveness sweep tracks
    the program through STARTING; waiting out a slow start would outlive the
    socket timeout. Raises SupervisorProgramActionError when supervisord
    refuses (an unknown program, a spawn error) or cannot be reached.
    """
    try:
        _supervisor_proxy(socket_path).supervisor.startProcess(program, False)
    except xmlrpc.client.Fault as e:
        if e.faultCode == _FAULT_ALREADY_STARTED:
            return
        raise SupervisorProgramActionError(f"supervisord refused to start {program!r}: {e.faultString}") from e
    except (OSError, xmlrpc.client.ProtocolError, xmlrpc.client.ResponseError) as e:
        raise SupervisorProgramActionError(f"could not reach supervisord to start {program!r}: {e}") from e


def stop_supervisor_program(program: str, socket_path: Path) -> None:
    """Ask supervisord to stop ``program``. Idempotent: not-running is success.

    ``wait=False`` for the same reason as the start: waiting out stopwaitsecs
    (10s by default) would outlive the socket timeout, and the liveness sweep
    tracks the program through STOPPING anyway.
    """
    try:
        _supervisor_proxy(socket_path).supervisor.stopProcess(program, False)
    except xmlrpc.client.Fault as e:
        if e.faultCode == _FAULT_NOT_RUNNING:
            return
        raise SupervisorProgramActionError(f"supervisord refused to stop {program!r}: {e.faultString}") from e
    except (OSError, xmlrpc.client.ProtocolError, xmlrpc.client.ResponseError) as e:
        raise SupervisorProgramActionError(f"could not reach supervisord to stop {program!r}: {e}") from e


def fetch_supervisor_program_states(socket_path: Path) -> dict[str, bool] | None:
    """Every supervised program's up/down state in one getAllProcessInfo RPC.

    Returns None when supervisord cannot be reached (or answers with something
    unmarshallable), so the caller falls back to per-row TCP probes rather than
    presenting a guess as supervisord's answer. Keys are bare program names --
    the same names ``forward_port.py --program`` registers and the per-program
    RPCs use (this config defines no supervisord groups).
    """
    try:
        # ``object`` collapses the marshallable union the proxy stub infers, so
        # the isinstance checks below are the narrowing the reads rely on.
        process_infos: object = _supervisor_proxy(socket_path).supervisor.getAllProcessInfo()
    except xmlrpc.client.Fault as e:
        _loguru_logger.debug("Supervisord refused getAllProcessInfo: {}", e.faultString)
        return None
    except (OSError, xmlrpc.client.ProtocolError, xmlrpc.client.ResponseError) as e:
        _loguru_logger.debug("Failed to reach supervisord for getAllProcessInfo: {}", e)
        return None
    if not isinstance(process_infos, list):
        return None
    is_running_by_program: dict[str, bool] = {}
    for process_info in process_infos:
        if not isinstance(process_info, dict):
            continue
        # Read the two keys by iteration: the proxy stub's inferred dict
        # variants make every keyed access an overload mismatch, while an
        # argument-free ``items()`` walk types cleanly on all of them.
        program_name = ""
        statename = ""
        for key, value in process_info.items():
            if key == "name":
                program_name = str(value)
            elif key == "statename":
                statename = str(value)
            else:
                pass
        if program_name:
            is_running_by_program[program_name] = statename in _RUNNING_STATE_NAMES
    return is_running_by_program


def probe_all_app_liveness(probe_targets: Sequence[tuple[str, str, str]]) -> dict[str, bool]:
    """Derive ``is_running`` for every registry row in one sweep.

    One batched supervisord RPC answers for all supervised rows, instead of one
    unix-socket round trip per row per sweep. A row falls back to the TCP probe
    when supervisord cannot answer at all, does not know the row's program, or
    the row is unsupervised (no program) -- the same per-row semantics as
    :func:`probe_app_liveness`.
    """
    is_running_by_program = fetch_supervisor_program_states(supervisor_socket_path())
    is_running_by_name: dict[str, bool] = {}
    for name, program, url in probe_targets:
        supervised_state = is_running_by_program.get(program) if is_running_by_program is not None else None
        if program and supervised_state is not None:
            is_running_by_name[name] = supervised_state
        else:
            is_running_by_name[name] = probe_tcp_url(url)
    return is_running_by_name


def probe_supervisor_program(program: str, socket_path: Path) -> bool | None:
    """Whether supervisord reports ``program`` as up, or None when it cannot say.

    None covers both an unreachable supervisord (no socket -- a dev setup, a
    test) and a program name supervisord does not know (a hand-edited registry,
    or a block removed since registration); the caller falls back to the TCP
    probe rather than presenting a guess as supervisord's answer.
    """
    try:
        # ``object`` collapses the marshallable union the proxy stub infers, so
        # the isinstance below is the one narrowing the read relies on.
        process_info: object = _supervisor_proxy(socket_path).supervisor.getProcessInfo(program)
    except xmlrpc.client.Fault as e:
        _loguru_logger.debug("Supervisord has no program {!r}: {}", program, e.faultString)
        return None
    except (OSError, xmlrpc.client.ProtocolError, xmlrpc.client.ResponseError) as e:
        _loguru_logger.debug("Failed to reach supervisord for {!r}: {}", program, e)
        return None
    if not isinstance(process_info, dict):
        return None
    # Read the one key by iteration: the proxy stub's inferred dict variants
    # make every keyed access (``get``, ``in``, subscript) an overload mismatch,
    # while an argument-free ``items()`` walk types cleanly on all of them.
    statename = ""
    for key, value in process_info.items():
        if key == "statename":
            statename = str(value)
    return statename in _RUNNING_STATE_NAMES


def probe_tcp_url(url: str) -> bool:
    """Whether anything accepts a TCP connect on ``url``'s host and port."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    if host is None:
        return False
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def probe_app_liveness(program: str, url: str) -> bool:
    """The ``is_running`` derivation for one registry row.

    Supervisord's process state for a supervised row, with the TCP probe as the
    fallback whenever supervisord cannot answer (and the whole story for an
    unsupervised row).
    """
    if program:
        supervised_state = probe_supervisor_program(program, supervisor_socket_path())
        if supervised_state is not None:
            return supervised_state
    return probe_tcp_url(url)
