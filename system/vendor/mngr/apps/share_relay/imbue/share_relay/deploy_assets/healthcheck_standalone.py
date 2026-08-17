"""Standalone relay liveness endpoint (stdlib only; ships to /usr/local/bin on the relay).

Mirror of ``imbue.share_relay.healthcheck`` without any package imports, so the
relay host needs nothing beyond the system python3. ``GET /healthz`` answers
200 while the local frps tunnel-control port accepts TCP connections, 503
otherwise. Ports come from SHARE_RELAY_HEALTHCHECK_PORT /
SHARE_RELAY_TUNNEL_CONTROL_PORT (defaults 8080 / 7000).
"""

import os
import socket
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

_HEALTH_PATH = "/healthz"
_PROBE_CONNECT_TIMEOUT_SECONDS = 2.0


def is_tunnel_control_port_accepting(tunnel_control_port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", tunnel_control_port), timeout=_PROBE_CONNECT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


class HealthcheckHandler(BaseHTTPRequestHandler):
    """Answers ``GET /healthz`` with 200 when frps is up, 503 otherwise."""

    tunnel_control_port: int = 7000

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
        pass


def build_server(healthcheck_port: int, tunnel_control_port: int) -> ThreadingHTTPServer:
    HealthcheckHandler.tunnel_control_port = tunnel_control_port
    return ThreadingHTTPServer(("0.0.0.0", healthcheck_port), HealthcheckHandler)


def main() -> None:
    healthcheck_port = int(os.environ.get("SHARE_RELAY_HEALTHCHECK_PORT", "8080"))
    tunnel_control_port = int(os.environ.get("SHARE_RELAY_TUNNEL_CONTROL_PORT", "7000"))
    build_server(healthcheck_port, tunnel_control_port).serve_forever()


if __name__ == "__main__":
    main()
