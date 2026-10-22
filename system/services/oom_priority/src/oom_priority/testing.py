"""Shared helpers for the tests of this package and of the programs that read through it.

The cgroup headroom publisher's policy (``cgroup_meminfo``) and the supervisord program that
runs it (``bin/publish_cgroup_meminfo.py``) are tested against the same fake cgroup v2 memory
controller; the supervisord socket reader (``supervisor_rpc``) and the System Monitor app's
Stop actions against the same stand-in for supervisord's control socket.
"""

import socketserver
import tempfile
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from xmlrpc.server import SimpleXMLRPCDispatcher, SimpleXMLRPCRequestHandler


def write_fake_cgroup(
    cgroup_dir: Path, limit: str, usage: str, stat_lines: dict[str, int]
) -> None:
    """Lay out a cgroup v2 memory controller: ``memory.max``, ``memory.current`` and ``memory.stat``.

    ``limit`` and ``usage`` are the files' raw text (``"max"`` for an uncapped cgroup).
    """
    cgroup_dir.mkdir(parents=True, exist_ok=True)
    (cgroup_dir / "memory.max").write_text(f"{limit}\n")
    (cgroup_dir / "memory.current").write_text(f"{usage}\n")
    (cgroup_dir / "memory.stat").write_text(
        "".join(f"{key} {value}\n" for key, value in stat_lines.items())
    )


class _UnixRequestHandler(SimpleXMLRPCRequestHandler):
    # The stock handler sets a TCP-only socket option, which a Unix socket refuses.
    disable_nagle_algorithm = False


class _UnixXMLRPCServer(socketserver.UnixStreamServer, SimpleXMLRPCDispatcher):
    def __init__(self, path: str) -> None:
        SimpleXMLRPCDispatcher.__init__(self, allow_none=True)
        socketserver.UnixStreamServer.__init__(self, path, _UnixRequestHandler)
        self.logRequests = False


@contextmanager
def fake_supervisord(**methods: Callable[..., Any]) -> Generator[Path, None, None]:
    """A stand-in for supervisord's control socket that answers the given XML-RPC methods, served on a
    thread for the duration of the block; yields the socket's path.

    Each keyword names a method of the ``supervisor`` namespace (``stopProcess=...`` answers
    ``supervisor.stopProcess``). The socket sits in a short directory under ``/tmp``, since a Unix
    socket path cannot be long; a sibling path there (``sock.with_name(...)``) is a socket nobody serves.
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as short_dir:
        sock = Path(short_dir) / "s.sock"
        server = _UnixXMLRPCServer(str(sock))
        for name, method in methods.items():
            server.register_function(method, f"supervisor.{name}")
        serving = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}
        )
        serving.start()
        try:
            yield sock
        finally:
            server.shutdown()
            serving.join(timeout=5)
            server.server_close()
