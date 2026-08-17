import httpx
import pytest

from imbue.share_relay.dns_records import RelayDnsError
from imbue.share_relay.dns_records import reconcile_a_record_set
from imbue.share_relay.dns_records import relay_dns_record_names


def test_relay_dns_record_names_cover_relay_host_and_content_wildcard() -> None:
    assert relay_dns_record_names("us1.minds-test.example") == [
        "relay.us1.minds-test.example",
        "*.us1.minds-test.example",
    ]


def test_reconcile_a_record_set_creates_missing_records() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json={"success": True, "result": []})
        body = request.read().decode()
        assert '"proxied": false' in body or '"proxied":false' in body
        return httpx.Response(200, json={"success": True, "result": {"id": "rec-new"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        changed = reconcile_a_record_set(client, "zone-1", "*.us1.minds-test.example", ["203.0.113.7", "203.0.113.8"])

    assert changed is True
    assert [method for method, _path in seen] == ["GET", "POST", "POST"]


def test_reconcile_a_record_set_is_a_noop_when_the_set_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": [
                    {"id": "rec-1", "content": "203.0.113.7"},
                    {"id": "rec-2", "content": "203.0.113.8"},
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        changed = reconcile_a_record_set(client, "zone-1", "*.us1.minds-test.example", ["203.0.113.8", "203.0.113.7"])

    assert changed is False


def test_reconcile_a_record_set_adds_and_deletes_to_converge() -> None:
    # A dead relay's IP leaves the set and a fresh relay's joins it, in one pass.
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [
                        {"id": "rec-keep", "content": "203.0.113.7"},
                        {"id": "rec-stale", "content": "203.0.113.9"},
                    ],
                },
            )
        return httpx.Response(200, json={"success": True, "result": {"id": "rec-x"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        changed = reconcile_a_record_set(
            client, "zone-1", "relay.us1.minds-test.example", ["203.0.113.7", "203.0.113.8"]
        )

    assert changed is True
    assert seen[1:] == [
        ("POST", "/client/v4/zones/zone-1/dns_records"),
        ("DELETE", "/client/v4/zones/zone-1/dns_records/rec-stale"),
    ]


def test_reconcile_a_record_set_refuses_an_empty_ip_set() -> None:
    # Emptying a region's record set would take every share in it down even
    # harder than a dead relay; the caller must always pass at least one IP.
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        with pytest.raises(RelayDnsError, match="empty IP set"):
            reconcile_a_record_set(client, "zone-1", "*.us1.minds-test.example", [])


def test_reconcile_a_record_set_raises_on_cloudflare_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"success": False, "errors": [{"code": 9109}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RelayDnsError):
            reconcile_a_record_set(client, "zone-1", "relay.us1.minds-test.example", ["203.0.113.7"])


def test_reconcile_a_record_set_raises_on_non_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RelayDnsError, match="status 502"):
            reconcile_a_record_set(client, "zone-1", "relay.us1.minds-test.example", ["203.0.113.7"])
