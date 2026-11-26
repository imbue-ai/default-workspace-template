import threading
import urllib.request

from imbue.system_interface.testing import listening_loopback_socket
from imbue.system_interface.testing import stand_in_app
from imbue.system_interface.wsgi import make_threaded_server


def test_a_threaded_server_serves_http_1_1_on_a_listening_socket_it_adopts() -> None:
    with listening_loopback_socket() as listener:
        port = listener.getsockname()[1]
        server = make_threaded_server("127.0.0.1", port, stand_in_app(), fd=listener.fileno())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5.0) as response:
                version, body = response.version, response.read()
        finally:
            server.shutdown()
            thread.join(timeout=5.0)
            server.server_close()

    assert (version, body) == (11, b"ok")
