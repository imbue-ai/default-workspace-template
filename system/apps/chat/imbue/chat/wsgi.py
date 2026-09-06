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


class Http11RequestHandler(WSGIRequestHandler):
    """Werkzeug request handler pinned to HTTP/1.1.

    The default dev-server handler speaks HTTP/1.0, which disables keepalive and
    forces a connection close after each response -- breaking long-lived
    Server-Sent Events streams and the per-connection keepalive that the
    WebSocket endpoints rely on. HTTP/1.1 enables persistent connections and
    chunked transfer encoding so streamed responses flush incrementally.
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
    endpoints (the ``/api/ws`` broadcaster and the proto-agent-logs stream)
    offer no subprotocol, so the negotiation loop never runs and no
    subprotocol is echoed -- the passthrough is inert for them but keeps the
    server permissive for subprotocol-bearing clients.
    """

    def __contains__(self, _subprotocol: object) -> bool:
        return True


def build_sock(application: Flask) -> Sock:
    """Wire flask-sock onto ``application`` with the keepalive and subprotocol policy both documents share."""
    application.config["SOCK_SERVER_OPTIONS"] = {
        "ping_interval": WS_PING_INTERVAL_SECONDS,
        # Echo back whatever subprotocol a client offers so subprotocol-bearing
        # WS clients can connect; see ``ReflectClientSubprotocols``.
        "subprotocols": ReflectClientSubprotocols(),
    }
    return Sock(application)


def make_threaded_server(host: str, port: int, app: Flask) -> BaseWSGIServer:
    """Build a threaded Werkzeug server that serves HTTP/1.1.

    Thread-per-connection (so flask-sock and long-lived SSE/WebSocket
    connections each own a thread) plus HTTP/1.1 keepalive and chunked
    streaming. The caller owns the returned server's ``serve_forever`` /
    ``shutdown`` lifecycle.
    """
    return make_server(host, port, app, threaded=True, request_handler=Http11RequestHandler)
