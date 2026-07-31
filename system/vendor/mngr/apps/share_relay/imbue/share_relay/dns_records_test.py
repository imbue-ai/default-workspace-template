import httpx
import pytest
from pydantic import AnyHttpUrl

from imbue.share_relay.data_types import RelayConfiguration
from imbue.share_relay.dns_records import RelayDnsError
from imbue.share_relay.dns_records import relay_dns_record_names
from imbue.share_relay.dns_records import upsert_a_record
from imbue.share_relay.primitives import ContentDomain
from imbue.share_relay.primitives import RegionCode


def _config() -> RelayConfiguration:
    return RelayConfiguration(
        region=RegionCode("us1"),
        content_domain=ContentDomain("minds-test.example"),
        plugin_auth_url=AnyHttpUrl("https://connector.example/frps/auth/secret-1"),
    )


def test_relay_dns_record_names_cover_relay_host_and_content_wildcard() -> None:
    assert relay_dns_record_names(_config()) == [
        "relay.us1.minds-test.example",
        "*.us1.minds-test.example",
    ]


def test_upsert_a_record_creates_when_absent() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json={"success": True, "result": []})
        body = request.read().decode()
        assert '"proxied": false' in body or '"proxied":false' in body
        return httpx.Response(200, json={"success": True, "result": {"id": "rec-new"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        record_id = upsert_a_record(client, "zone-1", "*.us1.minds-test.example", "203.0.113.7")

    assert record_id == "rec-new"
    assert [method for method, _path in seen] == ["GET", "POST"]


def test_upsert_a_record_updates_when_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"success": True, "result": [{"id": "rec-old"}]})
        assert request.method == "PUT"
        assert request.url.path.endswith("/rec-old")
        return httpx.Response(200, json={"success": True, "result": {"id": "rec-old"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        record_id = upsert_a_record(client, "zone-1", "relay.us1.minds-test.example", "203.0.113.7")

    assert record_id == "rec-old"


def test_upsert_a_record_deletes_stale_duplicates() -> None:
    # Leftover duplicates (e.g. old round-robin entries) would keep answering
    # with the old IP; the upsert must converge on exactly one A record.
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200, json={"success": True, "result": [{"id": "rec-old"}, {"id": "rec-dup1"}, {"id": "rec-dup2"}]}
            )
        return httpx.Response(200, json={"success": True, "result": {"id": "rec-old"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        record_id = upsert_a_record(client, "zone-1", "relay.us1.minds-test.example", "203.0.113.7")

    assert record_id == "rec-old"
    assert seen[1:] == [
        ("PUT", "/client/v4/zones/zone-1/dns_records/rec-old"),
        ("DELETE", "/client/v4/zones/zone-1/dns_records/rec-dup1"),
        ("DELETE", "/client/v4/zones/zone-1/dns_records/rec-dup2"),
    ]


def test_upsert_a_record_raises_on_cloudflare_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"success": False, "errors": [{"code": 9109}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RelayDnsError):
            upsert_a_record(client, "zone-1", "relay.us1.minds-test.example", "203.0.113.7")


def test_upsert_a_record_raises_on_non_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RelayDnsError, match="status 502"):
            upsert_a_record(client, "zone-1", "relay.us1.minds-test.example", "203.0.113.7")
