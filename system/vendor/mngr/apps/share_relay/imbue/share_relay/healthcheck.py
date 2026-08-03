"""A trivial liveness endpoint for a relay host.

A dead relay takes every share in its region down, so an external monitor pings
this endpoint. It reports whether the local frps tunnel-control port is
accepting connections -- the one signal that means "this relay can still take
workspace tunnels". Monitoring/alerting wiring is deferred (tier 3); this only
answers the probe.
"""

import socket
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Final

from loguru import logger

from imbue.share_relay.primitives import RelayPort

_HEALTH_PATH: Final[str] = "/healthz"
_PROBE_CONNECT_TIMEOUT_SECONDS: Final[float] = 2.0


def is_tunnel_control_port_accepting(tunnel_control_port: RelayPort) -> bool:
    """True if the local frps tunnel-control port accepts a TCP connection."""
    try:
        with socket.create_connection(("127.0.0.1", int(tunnel_control_port)), timeout=_PROBE_CONNECT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


class _HealthcheckHandler(BaseHTTPRequestHandler):
    """Answers ``GET /healthz`` with 200 when frps is up, 503 otherwise."""

    # Set by ``serve_healthcheck`` before the server starts; every request reads it.
    tunnel_control_port: RelayPort = RelayPort(1)

    def do_GET(self) -> None:
        if self.path != _HEALTH_PATH:
            self.send_response(404)
            self.end_headers()
            return
        is_healthy = is_tunnel_control_port_accepting(self.tunnel_control_port)
        self.send_response(200 if is_healthy else 503)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok\n" if is_healthy else b"frps unreachable\n")

    def log_message(self, format: str, *args: object) -> None:
        # BaseHTTPRequestHandler logs every request to stderr by default; route
        # it through loguru at trace so a health probe does not spam the logs.
        logger.trace("healthcheck {}", format % args)


def build_healthcheck_server(healthcheck_port: RelayPort, tunnel_control_port: RelayPort) -> ThreadingHTTPServer:
    """Build (but do not start) the healthcheck HTTP server.

    Split from ``serve_healthcheck`` so a caller can drive ``serve_forever`` /
    ``shutdown`` on its own thread (tests, and any embedding that needs a clean
    stop) instead of blocking forever.
    """
    _HealthcheckHandler.tunnel_control_port = tunnel_control_port
    return ThreadingHTTPServer(("0.0.0.0", int(healthcheck_port)), _HealthcheckHandler)


def serve_healthcheck(healthcheck_port: RelayPort, tunnel_control_port: RelayPort) -> None:
    """Run the healthcheck HTTP server forever (blocking)."""
    server = build_healthcheck_server(healthcheck_port, tunnel_control_port)
    logger.info("Serving relay healthcheck on :{}{}", healthcheck_port, _HEALTH_PATH)
    server.serve_forever()
