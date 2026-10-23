import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from werkzeug.serving import make_server
from werkzeug.wrappers import Request
from werkzeug.wrappers import Response

from share_gateway.turn import REFUSAL_RETRY_INTERVAL
from share_gateway.turn import TurnCredential
from share_gateway.turn import TurnCredentialCache
from share_gateway.turn import fetch_turn_credential
from share_gateway.turn import parse_ice_servers_answer

_T0 = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
_SERVERS: list[object] = [
    {"urls": ["stun:stun.cloudflare.com:3478"]},
    {"urls": ["turn:turn.cloudflare.com:3478?transport=udp"], "username": "u", "credential": "c"},
]


class _FakeConnector:
    """Answers each ``GET /turn/ice-servers`` from a script of (status, body) answers, recording the requests."""

    def __init__(self, answers: list[tuple[int, object]]) -> None:
        self.answers = list(answers)
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def application(self, request: Request) -> Response:
        with self._lock:
            self.requests.append((request.path, request.headers.get("Authorization", "")))
            status, body = self.answers.pop(0) if self.answers else (500, {"detail": "script exhausted"})
        return Response(json.dumps(body), status=status, mimetype="application/json")


@contextmanager
def _serve(connector: _FakeConnector) -> Generator[str, None, None]:
    server = make_server("127.0.0.1", 0, Request.application(connector.application))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def test_fetch_carries_the_relay_token_and_reads_the_credential() -> None:
    connector = _FakeConnector([(200, {"ice_servers": _SERVERS, "expires_at": "2026-10-17T13:00:00Z"})])
    with _serve(connector) as url:
        credential = fetch_turn_credential(url, "tok-relay", _T0)
    assert credential == TurnCredential(ice_servers=_SERVERS, fetched_at=_T0, expires_at=_T0 + timedelta(hours=1))
    assert connector.requests == [("/turn/ice-servers", "Bearer tok-relay")]


def test_a_structured_refusal_and_an_unreachable_connector_answer_no_credential() -> None:
    connector = _FakeConnector([(403, {"detail": {"code": "turn_quota_exceeded", "message": "over quota"}})])
    with _serve(connector) as url:
        assert fetch_turn_credential(url, "tok", _T0) is None
    assert fetch_turn_credential("http://127.0.0.1:9", "tok", _T0) is None


def test_parse_defaults_an_unreadable_expiry_to_the_one_hour_ttl() -> None:
    parsed = parse_ice_servers_answer({"ice_servers": _SERVERS, "expires_at": "soon"}, _T0)
    assert parsed is not None
    assert parsed.expires_at == _T0 + timedelta(hours=1)
    assert parsed.refresh_at == _T0 + timedelta(minutes=30)
    assert parse_ice_servers_answer({"ice_servers": "nope"}, _T0) is None
    assert parse_ice_servers_answer([], _T0) is None


def test_cache_fetches_at_start_and_again_at_half_life() -> None:
    connector = _FakeConnector(
        [
            (200, {"ice_servers": _SERVERS, "expires_at": "2026-10-17T13:00:00Z"}),
            (200, {"ice_servers": _SERVERS[:1], "expires_at": "2026-10-17T13:30:00Z"}),
        ]
    )
    clock = _Clock(_T0)
    with _serve(connector) as url:
        cache = TurnCredentialCache(url, "tok", now=clock)
        assert cache.refresh_if_due() is True
        assert cache.ice_servers() == _SERVERS
        clock.now = _T0 + timedelta(minutes=29)
        assert cache.refresh_if_due() is False
        clock.now = _T0 + timedelta(minutes=30)
        assert cache.refresh_if_due() is True
        assert cache.ice_servers() == _SERVERS[:1]
    assert len(connector.requests) == 2


def test_a_refusal_leaves_an_empty_list_and_waits_before_asking_again() -> None:
    connector = _FakeConnector(
        [
            (429, {"detail": {"code": "turn_rate_limited", "message": "slow down"}}),
            (200, {"ice_servers": _SERVERS, "expires_at": "2026-10-17T14:00:00Z"}),
        ]
    )
    clock = _Clock(_T0)
    with _serve(connector) as url:
        cache = TurnCredentialCache(url, "tok", now=clock)
        assert cache.refresh_if_due() is True
        assert cache.ice_servers() == []
        clock.now = _T0 + REFUSAL_RETRY_INTERVAL - timedelta(seconds=1)
        assert cache.refresh_if_due() is False
        clock.now = _T0 + REFUSAL_RETRY_INTERVAL
        assert cache.refresh_if_due() is True
        assert cache.ice_servers() == _SERVERS
    assert len(connector.requests) == 2


def test_a_refused_refresh_keeps_the_held_credential_until_it_expires() -> None:
    connector = _FakeConnector(
        [
            (200, {"ice_servers": _SERVERS, "expires_at": "2026-10-17T13:00:00Z"}),
            (503, {"detail": {"code": "turn_unavailable", "message": "no key"}}),
            (503, {"detail": {"code": "turn_unavailable", "message": "no key"}}),
        ]
    )
    clock = _Clock(_T0)
    with _serve(connector) as url:
        cache = TurnCredentialCache(url, "tok", now=clock)
        cache.refresh_if_due()
        clock.now = _T0 + timedelta(minutes=31)
        assert cache.refresh_if_due() is True
        assert cache.ice_servers() == _SERVERS
        # The refusal is believed for the retry interval even though the credential is past its half life.
        clock.now = _T0 + timedelta(minutes=31, seconds=10)
        assert cache.refresh_if_due() is False
        clock.now = _T0 + timedelta(minutes=31) + REFUSAL_RETRY_INTERVAL - timedelta(seconds=1)
        assert cache.refresh_if_due() is False
        assert cache.ice_servers() == _SERVERS
        clock.now = _T0 + timedelta(hours=1)
        assert cache.ice_servers() == []
        assert cache.refresh_if_due() is True
        assert cache.credential is None
    assert len(connector.requests) == 3
