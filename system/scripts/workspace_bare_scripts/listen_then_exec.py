#!/usr/bin/env python3
"""Bind an app's port before the app imports anything, then exec the app on that socket.

The shell and the chat app spend their first seconds importing. Until they bind, a request
routed to them is refused, and whatever sent it falls back to polling. This script runs first in
their program line (``python3 <this script> 127.0.0.1:8000 <command...>``): it binds and listens
on the port at once, leaves the socket open across the exec, names its descriptor in
``WERKZEUG_SERVER_FD`` and execs the command. The app's server (``make_threaded_server`` in the
app's ``wsgi.py``) serves on that socket instead of binding its own. A connection that arrives in
the meantime waits in the listen backlog and is answered as soon as the app serves, so the
program line can register and announce the app before the app has imported anything.

Stdlib only: supervisord runs it under the system ``python3``.
"""

from __future__ import annotations

import os
import socket
import sys
from collections.abc import Sequence

# werkzeug's own name for a listening socket handed down across an exec (``run_simple`` reads it);
# the apps' ``make_threaded_server`` reads it the same way.
INHERITED_SOCKET_ENV = "WERKZEUG_SERVER_FD"
# werkzeug's own backlog (``werkzeug.serving.LISTEN_QUEUE``).
LISTEN_BACKLOG = 128


def parse_address(address: str) -> tuple[str, int]:
    """``host:port`` as a (host, port) pair; raises ValueError for anything else."""
    host, separator, port = address.rpartition(":")
    if not separator or not host or not port.isdigit():
        raise ValueError(f"expected host:port, got {address!r}")
    return host, int(port)


def bind_listener(host: str, port: int) -> socket.socket:
    """A socket listening on ``host``:``port`` that the exec'd command inherits."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(LISTEN_BACKLOG)
    listener.set_inheritable(True)
    return listener


def main(argv: Sequence[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 2:
        print(
            "usage: listen_then_exec.py <host:port> <command> [args...]",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        host, port = parse_address(arguments[0])
    except ValueError as error:
        print(f"listen_then_exec.py: {error}", file=sys.stderr)
        sys.exit(2)
    try:
        listener = bind_listener(host, port)
    except OSError as error:
        print(
            f"listen_then_exec.py: cannot listen on {host}:{port}: {error}",
            file=sys.stderr,
        )
        sys.exit(1)
    command = arguments[1:]
    os.execvpe(
        command[0],
        command,
        {**os.environ, INHERITED_SOCKET_ENV: str(listener.fileno())},
    )


if __name__ == "__main__":
    main()
