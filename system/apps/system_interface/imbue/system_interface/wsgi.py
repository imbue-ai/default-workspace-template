import os
from typing import Final

from flask import Flask
from flask_sock import Sock
from werkzeug.serving import BaseWSGIServer
from werkzeug.serving import WSGIRequestHandler
from werkzeug.serving import make_server

# How often flask-sock sends a keepalive ping on each WebSocket connection.
# Pings detect (and tear down) half-dead peers without any asyncio machinery --
# each connection owns its own thread, so a wedged send only stalls that thread.
WS_PING_INTERVAL_SECONDS: Final[int] = 25

# The descriptor of a listening socket the program line bound before this process imported
# (system/scripts/workspace_bare_scripts/listen_then_exec.py), under werkzeug's own name for one
# handed down across an exec.
INHERITED_SOCKET_ENV: Final[str] = "WERKZEUG_SERVER_FD"


class InheritedSocketMismatchError(ValueError):
    """The socket the program line bound is not on the address this server was configured for."""


class Http11RequestHandler(WSGIRequestHandler):
    """Werkzeug request handler pinned to HTTP/1.1.

    The default dev-server handler speaks HTTP/1.0, which disables keepalive and
    forces a connection close after each response -- breaking the per-connection
    keepalive that the WebSocket endpoint relies on. HTTP/1.1 enables persistent
    connections and chunked transfer encoding so streamed responses flush
    incrementally.
    """

    protocol_version = "HTTP/1.1"


class ReflectClientSubprotocols:
    """A WebSocket subprotocols allow-list that accepts whatever the client offers.

    ``flask_sock`` builds one ``simple_websocket.Server`` per connection from
    ``SOCK_SERVER_OPTIONS`` and completes the WebSocket handshake (selecting and
    echoing the subprotocol) *before* our route handler runs, so a handler cannot
    choose the subprotocol per-connection. ``simple_websocket``'s default
    ``choose_subprotocol`` echoes the first client-offered subprotocol that is
    ``in`` this allow-list; making ``__contains__`` always true turns that into a
    transparent passthrough -- the server echoes back whatever subprotocol the
    client requested.

    Chrome aborts a WebSocket handshake (close 1006) if the client offered a
    subprotocol and the 101 response echoes none, so any future WS route that
    negotiates a subprotocol works without touching this list. Today's own
    endpoint (the ``/api/ws`` broadcaster) offers no subprotocol, so the
    negotiation loop never runs and no subprotocol is echoed -- the passthrough
    is inert for it but keeps the server permissive for subprotocol-bearing
    clients.
    """

    def __contains__(self, _subprotocol: object) -> bool:
        return True


def build_sock(application: Flask) -> Sock:
    """Wire flask-sock onto ``application`` with the keepalive and subprotocol policy the shell's WebSocket uses."""
    application.config["SOCK_SERVER_OPTIONS"] = {
        "ping_interval": WS_PING_INTERVAL_SECONDS,
        # Echo back whatever subprotocol a client offers so subprotocol-bearing
        # WS clients can connect; see ``ReflectClientSubprotocols``.
        "subprotocols": ReflectClientSubprotocols(),
    }
    return Sock(application)


def make_threaded_server(host: str, port: int, app: Flask) -> BaseWSGIServer:
    """Build a threaded Werkzeug server that serves HTTP/1.1.

    Thread-per-connection (so flask-sock and long-lived WebSocket connections
    each own a thread) plus HTTP/1.1 keepalive and chunked streaming. The
    caller owns the returned server's ``serve_forever`` / ``shutdown``
    lifecycle.

    When the program line bound the port already (``INHERITED_SOCKET_ENV`` names the socket),
    the server serves on that socket, so the connections that arrived while this process
    imported are answered rather than refused; it must be on ``host``:``port``.
    """
    inherited_fd = os.environ.pop(INHERITED_SOCKET_ENV, None)
    server = make_server(
        host,
        port,
        app,
        threaded=True,
        request_handler=Http11RequestHandler,
        fd=int(inherited_fd) if inherited_fd is not None else None,
    )
    if inherited_fd is not None:
        # werkzeug serves on a duplicate of the descriptor, so the inherited one is closed here.
        os.close(int(inherited_fd))
        bound_host, bound_port = server.socket.getsockname()[:2]
        if (bound_host, bound_port) != (host, port):
            server.server_close()
            raise InheritedSocketMismatchError(
                f"the inherited socket listens on {bound_host}:{bound_port}, not {host}:{port}"
            )
    return server
