import socket
import threading
from http.client import HTTPConnection

from imbue.share_relay.healthcheck import build_healthcheck_server
from imbue.share_relay.healthcheck import is_tunnel_control_port_accepting
from imbue.share_relay.primitives import RelayPort


def _free_port() -> RelayPort:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return RelayPort(probe.getsockname()[1])


def test_is_tunnel_control_port_accepting_true_when_listening() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = RelayPort(listener.getsockname()[1])
    try:
        assert is_tunnel_control_port_accepting(port) is True
    finally:
        listener.close()


def test_is_tunnel_control_port_accepting_false_when_closed() -> None:
    # Bind then immediately close to obtain a port nothing is listening on.
    port = _free_port()
    assert is_tunnel_control_port_accepting(port) is False


def _get_status(port: RelayPort, path: str = "/healthz") -> int:
    conn = HTTPConnection("127.0.0.1", int(port), timeout=2.0)
    try:
        conn.request("GET", path)
        return conn.getresponse().status
    finally:
        conn.close()


def test_healthcheck_reports_200_when_frps_up_and_503_when_down() -> None:
    # A stand-in for frps: a real listening socket the healthcheck probes.
    fake_frps = socket.socket()
    fake_frps.bind(("127.0.0.1", 0))
    fake_frps.listen(1)
    tunnel_control_port = RelayPort(fake_frps.getsockname()[1])

    server = build_healthcheck_server(_free_port(), tunnel_control_port)
    healthcheck_port = RelayPort(server.server_address[1])
    serve_thread = threading.Thread(target=server.serve_forever, name="healthcheck-server", daemon=True)
    serve_thread.start()
    try:
        # frps up -> 200; unknown path -> 404.
        assert _get_status(healthcheck_port) == 200
        assert _get_status(healthcheck_port, path="/nope") == 404

        # frps down -> 503.
        fake_frps.close()
        assert _get_status(healthcheck_port) == 503
    finally:
        server.shutdown()
        server.server_close()
        serve_thread.join(timeout=5.0)
