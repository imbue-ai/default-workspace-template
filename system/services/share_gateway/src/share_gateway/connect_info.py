"""What the gateway tells the Screen service over loopback ``GET /connect-info`` (the Screen spec, section 15).

``candidates`` is where a visitor can reach the Screen's ICE mux port: the box's own
published endpoints the connector reported with the assignment (a cloud workspace), plus
one TCP candidate per relay of the region at the share's ``media_port``, which every
relay's frpc claims as the ``screen-media`` tcp proxy to the mux's local port.
``ice_servers`` is the relay credential the gateway holds, possibly none.
"""

from share_gateway.assignment import RelayAssignment


def build_candidates(assignment: RelayAssignment, local_media_port: int) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = [
        {
            "host": endpoint.host,
            "port": endpoint.port,
            "protocol": endpoint.protocol,
            "guest_port": endpoint.guest_port,
        }
        for endpoint in assignment.published_endpoints
    ]
    if assignment.media_port is not None:
        for relay_id in sorted(assignment.endpoint_by_relay_id):
            relay_host, _relay_port = assignment.endpoint_by_relay_id[relay_id]
            candidates.append(
                {
                    "host": relay_host,
                    "port": assignment.media_port,
                    "protocol": "tcp",
                    "guest_port": local_media_port,
                }
            )
    deduplicated: list[dict[str, object]] = []
    for candidate in candidates:
        if candidate not in deduplicated:
            deduplicated.append(candidate)
    return deduplicated


def build_connect_info(
    assignment: RelayAssignment, ice_servers: list[object], local_media_port: int
) -> dict[str, object]:
    return {
        "ice_servers": ice_servers,
        "candidates": build_candidates(assignment, local_media_port),
    }
