"""The address the ``screen-media`` proxy delivers a visitor's connection to.

neko's ICE stack (pion) keys its TCP mux by the local address a connection arrives
on, and gathers a candidate only for the workspace's own interface address, never
for loopback. A connection spliced to ``127.0.0.1:59000`` is therefore accepted by
the socket and then read by nothing, so ICE never completes; delivered to the
interface address it is matched like a direct one. The address is the one the
kernel would route a packet to the internet from, found from a connected UDP
socket (no packet is sent), which is what pion's gathering sees too.
"""

import socket

# Any routable address serves as the probe target: the socket is connected, never written to, so nothing leaves
# the workspace, and the kernel answers the source address it would use for that route.
_PROBE_ADDRESS = ("198.51.100.1", 9)


def primary_address() -> str | None:
    """The workspace's own routable IPv4 address, or None when it has no route out at all."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(_PROBE_ADDRESS)
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    if address.startswith("127.") or address == "0.0.0.0":
        return None
    return address
