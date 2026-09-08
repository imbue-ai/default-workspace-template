import json

import httpx
import pytest

from imbue.observability.dns_records import TelemetryDnsError
from imbue.observability.dns_records import upsert_proxied_ingest_record
from imbue.observability.primitives import TelemetryHostname

_HOSTNAME = TelemetryHostname("telemetry.minds-test.example")


def test_upsert_creates_a_proxied_record_when_none_exists() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json={"success": True, "result": []})
        body = json.loads(request.read().decode())
        # Proxied (orange cloud) is the point: it is what lets the origin
        # firewall admit only Cloudflare's ranges.
        assert body["proxied"] is True
        assert body["ttl"] == 1
        assert body["content"] == "203.0.113.7"
        return httpx.Response(200, json={"success": True, "result": {"id": "rec-new"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        changed = upsert_proxied_ingest_record(client, "zone-1", _HOSTNAME, "203.0.113.7")

    assert changed is True
    assert [method for method, _path in seen] == ["GET", "POST"]


def test_upsert_is_a_noop_when_the_record_already_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={"success": True, "result": [{"id": "rec-1", "content": "203.0.113.7", "proxied": True}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        changed = upsert_proxied_ingest_record(client, "zone-1", _HOSTNAME, "203.0.113.7")

    assert changed is False


def test_upsert_repoints_the_record_on_instance_replacement() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"success": True, "result": [{"id": "rec-1", "content": "203.0.113.7", "proxied": True}]},
            )
        body = json.loads(request.read().decode())
        assert body["content"] == "203.0.113.8"
        return httpx.Response(200, json={"success": True, "result": {"id": "rec-1"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        changed = upsert_proxied_ingest_record(client, "zone-1", _HOSTNAME, "203.0.113.8")

    assert changed is True
    assert seen == [
        ("GET", "/client/v4/zones/zone-1/dns_records"),
        ("PUT", "/client/v4/zones/zone-1/dns_records/rec-1"),
    ]


def test_upsert_fixes_an_unproxied_record_in_place() -> None:
    # A gray-cloud record would expose the origin IP and bypass the edge; the
    # upsert converges it back to proxied even when the IP already matches.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"success": True, "result": [{"id": "rec-1", "content": "203.0.113.7", "proxied": False}]},
            )
        assert request.method == "PUT"
        return httpx.Response(200, json={"success": True, "result": {"id": "rec-1"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert upsert_proxied_ingest_record(client, "zone-1", _HOSTNAME, "203.0.113.7") is True


def test_upsert_deletes_stray_sibling_records() -> None:
    # Replacement is sequential single-writer; a leftover second A record from
    # a partial earlier pass must not keep answering with a dead origin.
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [
                        {"id": "rec-1", "content": "203.0.113.7", "proxied": True},
                        {"id": "rec-2", "content": "203.0.113.9", "proxied": True},
                    ],
                },
            )
        return httpx.Response(200, json={"success": True, "result": {"id": "x"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        changed = upsert_proxied_ingest_record(client, "zone-1", _HOSTNAME, "203.0.113.7")

    assert changed is True
    assert ("DELETE", "/client/v4/zones/zone-1/dns_records/rec-2") in seen


def test_upsert_raises_on_cloudflare_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"success": False, "errors": [{"code": 9109}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TelemetryDnsError):
            upsert_proxied_ingest_record(client, "zone-1", _HOSTNAME, "203.0.113.7")


def test_upsert_raises_on_non_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TelemetryDnsError, match="status 502"):
            upsert_proxied_ingest_record(client, "zone-1", _HOSTNAME, "203.0.113.7")
