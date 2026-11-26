import errno
import socket
from contextlib import closing

import pytest

from imbue.system_interface.testing import can_bind_loopback_port
from imbue.system_interface.testing import listening_loopback_socket


def test_a_listening_loopback_socket_holds_its_port_against_every_other_bind_until_it_closes() -> None:
    """The port is held from the moment it is picked, so no other bind in the process (a server's, which reuses
    addresses, included) can be handed it before the server that adopts the socket serves."""
    with listening_loopback_socket() as listener:
        port = listener.getsockname()[1]
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as rival:
            rival.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError) as refusal:
                rival.bind(("127.0.0.1", port))

    assert refusal.value.errno == errno.EADDRINUSE
    assert can_bind_loopback_port(port)
