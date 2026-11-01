from share_gateway.assignment import PublishedEndpoint
from share_gateway.assignment import RelayAssignment
from share_gateway.connect_info import build_connect_info

_RELAY_A = "relay-" + "a" * 16
_RELAY_B = "relay-" + "b" * 16
_BOX_UDP = PublishedEndpoint(host="203.0.113.5", port=59000, protocol="udp", guest_port=59000)
_BOX_TCP = PublishedEndpoint(host="203.0.113.5", port=59000, protocol="tcp", guest_port=59000)
_BOX_SSH = PublishedEndpoint(host="203.0.113.5", port=2222, protocol="tcp", guest_port=22)
_SERVERS: list[object] = [
    {"urls": ["turn:turn.cloudflare.com:3478?transport=udp"], "username": "u", "credential": "c"}
]


def _assignment(media_port: int | None, published_endpoints: list[PublishedEndpoint]) -> RelayAssignment:
    return RelayAssignment(
        endpoint_by_relay_id={_RELAY_B: ("relay-us1b.example", 7000), _RELAY_A: ("relay-us1a.example", 7000)},
        poll_seconds=60,
        media_port=media_port,
        published_endpoints=published_endpoints,
    )


def test_connect_info_offers_the_box_endpoints_and_one_tcp_candidate_per_relay_at_the_media_port() -> None:
    answer = build_connect_info(_assignment(40123, [_BOX_UDP, _BOX_TCP, _BOX_SSH]), _SERVERS, 59000)
    assert answer == {
        "ice_servers": _SERVERS,
        "candidates": [
            {"host": "203.0.113.5", "port": 59000, "protocol": "udp", "guest_port": 59000},
            {"host": "203.0.113.5", "port": 59000, "protocol": "tcp", "guest_port": 59000},
            {"host": "203.0.113.5", "port": 2222, "protocol": "tcp", "guest_port": 22},
            {"host": "relay-us1a.example", "port": 40123, "protocol": "tcp", "guest_port": 59000},
            {"host": "relay-us1b.example", "port": 40123, "protocol": "tcp", "guest_port": 59000},
        ],
    }


def test_connect_info_without_a_media_port_offers_no_relay_candidate() -> None:
    answer = build_connect_info(_assignment(None, [_BOX_UDP]), [], 59000)
    assert answer == {
        "ice_servers": [],
        "candidates": [{"host": "203.0.113.5", "port": 59000, "protocol": "udp", "guest_port": 59000}],
    }


def test_connect_info_for_a_local_workspace_is_the_relays_alone() -> None:
    candidates = build_connect_info(_assignment(40123, []), [], 59000)["candidates"]
    assert isinstance(candidates, list)
    assert [candidate["host"] for candidate in candidates] == ["relay-us1a.example", "relay-us1b.example"]
