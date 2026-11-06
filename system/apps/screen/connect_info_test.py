"""``connect-info`` over fake brokers (both, one, none, slow, refusing) and the two real brokers over a loopback
stand-in for the latchkey proxy and the share gateway."""

import json
import threading
import time
from collections.abc import Generator
from collections.abc import Mapping
from contextlib import contextmanager

import pytest
from flask.testing import FlaskClient
from werkzeug.serving import make_server
from werkzeug.wrappers import Request
from werkzeug.wrappers import Response

from mock_broker_test import FakeBroker
from screen.brokers import DesktopBroker
from screen.brokers import ShareGatewayBroker
from screen.brokers import parse_candidates
from screen.brokers import parse_ice_servers_answer
from screen.connect_info import ConnectInfoAggregator
from screen.connect_info import merge_connect_info
from screen.data_types import IceServer
from screen.data_types import IceServersAnswer
from screen.data_types import MediaCandidate
from screen.data_types import TransportProtocol
from screen.errors import ConnectivityBrokerError

_UDP = MediaCandidate(host="127.0.0.1", port=32771, protocol=TransportProtocol.UDP, guest_port=59000)
_TCP = MediaCandidate(host="127.0.0.1", port=32772, protocol=TransportProtocol.TCP, guest_port=59000)
_RELAY_TCP = MediaCandidate(host="relay-us1.example", port=40123, protocol=TransportProtocol.TCP, guest_port=59000)
_SSH = MediaCandidate(host="127.0.0.1", port=32770, protocol=TransportProtocol.TCP, guest_port=22)
_TURN = IceServer(urls=("turn:turn.cloudflare.com:3478?transport=udp",), username="u", credential="c")
_STUN = IceServer(urls="stun:stun.cloudflare.com:3478")


def _desktop() -> FakeBroker:
    return FakeBroker(
        name="desktop",
        candidates=(_UDP, _TCP, _SSH),
        ice_servers=IceServersAnswer(ice_servers=(_STUN, _TURN), expires_at="2026-10-17T13:00:00Z"),
    )


def _gateway() -> FakeBroker:
    return FakeBroker(
        name="share gateway",
        candidates=(_TCP, _RELAY_TCP),
        ice_servers=IceServersAnswer(ice_servers=(_TURN,), expires_at="2026-10-17T12:30:00Z"),
    )


def test_both_brokers_are_unioned_filtered_to_the_mux_port_and_deduplicated() -> None:
    aggregator = ConnectInfoAggregator(brokers=(_desktop(), _gateway()))
    answer = aggregator.fetch(is_relay=True)
    assert answer.candidates == (_UDP, _TCP, _RELAY_TCP)
    assert answer.ice_servers == (_STUN, _TURN, _TURN)
    assert answer.expires_at == "2026-10-17T12:30:00Z"


def test_without_relay_no_broker_is_asked_for_ice_servers() -> None:
    desktop = _desktop()
    gateway = _gateway()
    answer = ConnectInfoAggregator(brokers=(desktop, gateway)).fetch(is_relay=False)
    assert answer.ice_servers == ()
    assert answer.expires_at is None
    assert answer.candidates == (_UDP, _TCP, _RELAY_TCP)
    assert desktop.calls == ["candidates"]
    assert gateway.calls == ["candidates"]


def test_one_broker_absent_leaves_the_others_answer() -> None:
    refusing = FakeBroker(name="share gateway", is_refusing=True)
    answer = ConnectInfoAggregator(brokers=(_desktop(), refusing)).fetch(is_relay=True)
    assert answer.candidates == (_UDP, _TCP)
    assert answer.ice_servers == (_STUN, _TURN)
    assert answer.expires_at == "2026-10-17T13:00:00Z"


def test_no_brokers_answer_empty_lists() -> None:
    assert ConnectInfoAggregator(brokers=()).fetch(is_relay=True).document() == {
        "ice_servers": [],
        "candidates": [],
        "expires_at": None,
    }
    both_refusing = (FakeBroker(name="a", is_refusing=True), FakeBroker(name="b", is_refusing=True))
    assert ConnectInfoAggregator(brokers=both_refusing).fetch(is_relay=True).candidates == ()


def test_a_slow_broker_is_left_out_within_the_deadline() -> None:
    slow = _gateway()
    slow.release.clear()
    aggregator = ConnectInfoAggregator(
        brokers=(_desktop(), slow), deadline_seconds=0.3, ice_servers_deadline_seconds=0.3
    )
    started = time.monotonic()
    answer = aggregator.fetch(is_relay=True)
    elapsed = time.monotonic() - started
    slow.release.set()
    assert answer.candidates == (_UDP, _TCP)
    assert answer.ice_servers == (_STUN, _TURN)
    assert elapsed < 1.5


def test_relay_servers_get_a_longer_bound_than_candidates() -> None:
    slow = _gateway()
    slow.release.clear()
    aggregator = ConnectInfoAggregator(brokers=(slow,), deadline_seconds=0.2, ice_servers_deadline_seconds=3.0)
    threading.Timer(0.6, slow.release.set).start()
    started = time.monotonic()
    answer = aggregator.fetch(is_relay=True)
    elapsed = time.monotonic() - started
    assert answer.candidates == ()
    assert answer.ice_servers == (_TURN,)
    assert 0.5 < elapsed < 2.5


def test_merge_keeps_candidates_without_a_guest_port() -> None:
    bare = MediaCandidate(host="203.0.113.5", port=40123, protocol=TransportProtocol.TCP)
    merged = merge_connect_info(((bare, _SSH),), (), 59000)
    assert merged.candidates == (bare,)


def test_document_spells_the_protocol_lower_case_and_drops_absent_credential_keys() -> None:
    document = merge_connect_info(((_UDP,),), (IceServersAnswer(ice_servers=(_STUN,), expires_at=None),), 59000)
    assert document.document() == {
        "ice_servers": [{"urls": "stun:stun.cloudflare.com:3478"}],
        "candidates": [{"host": "127.0.0.1", "port": 32771, "protocol": "udp"}],
        "expires_at": None,
    }


def test_parse_candidates_compares_the_protocol_case_insensitively_and_skips_junk() -> None:
    parsed = parse_candidates(
        [
            {"host": "127.0.0.1", "port": 32771, "protocol": "UDP", "guest_port": 59000},
            {"host": "relay.example", "port": 40123, "protocol": "tcp"},
            {"host": "", "port": 1, "protocol": "tcp"},
            {"host": "x", "port": "many", "protocol": "tcp"},
            {"host": "x", "port": 5, "protocol": "sctp"},
            "junk",
        ]
    )
    assert parsed == (
        _UDP,
        MediaCandidate(host="relay.example", port=40123, protocol=TransportProtocol.TCP),
    )
    with pytest.raises(ConnectivityBrokerError):
        parse_candidates({"not": "a list"})


def test_parse_ice_servers_keeps_unknown_keys_and_skips_junk() -> None:
    answer = parse_ice_servers_answer(
        {
            "ice_servers": [{"urls": ["turns:t:443?transport=tcp"], "username": "u", "credential": "c", "x": 1}, 7],
            "expires_at": "2026-10-17T13:00:00Z",
        }
    )
    assert len(answer.ice_servers) == 1
    assert answer.ice_servers[0].model_dump(exclude_none=True) == {
        "urls": ("turns:t:443?transport=tcp",),
        "username": "u",
        "credential": "c",
        "x": 1,
    }
    assert answer.expires_at == "2026-10-17T13:00:00Z"
    assert parse_ice_servers_answer({"ice_servers": []}).expires_at is None
    with pytest.raises(ConnectivityBrokerError):
        parse_ice_servers_answer({"ice_servers": "nope"})


@contextmanager
def _serve(
    responses: Mapping[str, tuple[int, object]], seen: list[tuple[str, dict[str, str]]]
) -> Generator[str, None, None]:
    """A loopback server answering each path from ``responses`` and recording every request's path and headers."""

    @Request.application
    def application(request: Request) -> Response:
        seen.append((request.path, dict(request.headers)))
        status, body = responses.get(request.path, (404, {"detail": "no such route"}))
        return Response(json.dumps(body), status=status, mimetype="application/json")

    server = make_server("127.0.0.1", 0, application)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


def test_desktop_broker_reaches_both_routes_through_the_proxy_with_the_gateway_headers() -> None:
    seen: list[tuple[str, dict[str, str]]] = []
    routes = {
        "/minds-api-proxy/api/v1/agents/agent-1/media-endpoints": (
            200,
            {"candidates": [{"host": "127.0.0.1", "port": 32771, "protocol": "UDP", "guest_port": 59000}]},
        ),
        "/minds-api-proxy/api/v1/agents/agent-1/ice-servers": (
            200,
            {"ice_servers": [{"urls": "stun:stun.cloudflare.com:3478"}], "expires_at": "2026-10-17T13:00:00Z"},
        ),
    }
    with _serve(routes, seen) as url:
        broker = DesktopBroker.from_environ(
            {
                "LATCHKEY_GATEWAY": url + "/",
                "LATCHKEY_GATEWAY_PASSWORD": "pw",
                "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE": "jwt",
                "MNGR_AGENT_ID": "agent-1",
            }
        )
        assert broker is not None
        assert broker.fetch_candidates() == (_UDP,)
        assert broker.fetch_ice_servers().ice_servers == (_STUN,)
    assert [path for path, _ in seen] == [
        "/minds-api-proxy/api/v1/agents/agent-1/media-endpoints",
        "/minds-api-proxy/api/v1/agents/agent-1/ice-servers",
    ]
    assert seen[0][1]["X-Latchkey-Gateway-Password"] == "pw"
    assert seen[0][1]["X-Latchkey-Gateway-Permissions-Override"] == "jwt"


def test_desktop_broker_is_absent_without_a_gateway_or_an_agent_id() -> None:
    assert DesktopBroker.from_environ({"LATCHKEY_GATEWAY": "http://x", "LATCHKEY_GATEWAY_PASSWORD": "pw"}) is None
    assert DesktopBroker.from_environ({"MNGR_AGENT_ID": "agent-1"}) is None


def test_a_broker_refusal_is_a_broker_error() -> None:
    seen: list[tuple[str, dict[str, str]]] = []
    with _serve({"/connect-info": (503, {"detail": {"code": "turn_unavailable"}})}, seen) as url:
        broker = ShareGatewayBroker(url=url + "/connect-info")
        with pytest.raises(ConnectivityBrokerError):
            broker.fetch_candidates()
    unreachable = ShareGatewayBroker(url="http://127.0.0.1:9/connect-info")
    with pytest.raises(ConnectivityBrokerError):
        unreachable.fetch_ice_servers()


def test_share_gateway_broker_reads_candidates_and_ice_servers_from_one_route() -> None:
    seen: list[tuple[str, dict[str, str]]] = []
    body = {
        "ice_servers": [{"urls": ["turn:t:3478?transport=udp"], "username": "u", "credential": "c"}],
        "candidates": [
            {"host": "203.0.113.5", "port": 32772, "protocol": "tcp", "guest_port": 59000},
            {"host": "relay-us1.example", "port": 40123, "protocol": "tcp", "guest_port": 59000},
        ],
    }
    with _serve({"/connect-info": (200, body)}, seen) as url:
        broker = ShareGatewayBroker(url=url + "/connect-info")
        assert [candidate.host for candidate in broker.fetch_candidates()] == ["203.0.113.5", "relay-us1.example"]
        assert broker.fetch_ice_servers() == IceServersAnswer(
            ice_servers=(IceServer(urls=("turn:t:3478?transport=udp",), username="u", credential="c"),),
            expires_at=None,
        )


def test_the_route_answers_empty_lists_with_both_brokers_silent_and_forbids_caching(client: FlaskClient) -> None:
    answer = client.get("/api/connect-info?relay=1")
    assert answer.status_code == 200
    assert answer.get_json() == {"ice_servers": [], "candidates": [], "expires_at": None}
    assert answer.headers["Cache-Control"] == "no-store"
