"""Tests for the relay fleet inventory: pure helpers + the /admin/relays API."""

import pytest

from imbue.remote_service_connector.errors import InvalidRelayRecordError
from imbue.remote_service_connector.errors import NoActiveRelaysError
from imbue.remote_service_connector.relays import eligible_regions
from imbue.remote_service_connector.relays import generate_relay_id
from imbue.remote_service_connector.relays import pick_fallback_region
from imbue.remote_service_connector.relays import relay_endpoints_for_region
from imbue.remote_service_connector.relays import validate_relay_id
from imbue.remote_service_connector.relays import validate_relay_ip_address
from imbue.remote_service_connector.relays import validate_relay_region
from imbue.remote_service_connector.relays import validate_tunnel_endpoint
from imbue.remote_service_connector.testing import _RELAY_ENDPOINT_US1
from imbue.remote_service_connector.testing import _RELAY_ID_US1
from imbue.remote_service_connector.testing import _RELAY_ID_US2
from imbue.remote_service_connector.testing import _make_share_test_client
from imbue.remote_service_connector.testing import make_relay_row

_ADMIN_KEY = "test-admin-key-4fb0e2"


def _admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_ADMIN_KEY}"}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_generate_relay_id_is_valid_and_unique() -> None:
    first, second = generate_relay_id(), generate_relay_id()
    assert first != second
    assert validate_relay_id(first) == first


def test_validate_relay_id_normalizes_and_rejects_bad_shapes() -> None:
    assert validate_relay_id("  RELAY-ABCD1234  ") == "relay-abcd1234"
    for bad in ("relay-", "relay-xyz!", "notrelay-abcd1234", "relay-" + "a" * 33, ""):
        with pytest.raises(InvalidRelayRecordError):
            validate_relay_id(bad)


def test_validate_relay_region_accepts_labels_and_rejects_others() -> None:
    assert validate_relay_region(" US1 ") == "us1"
    assert validate_relay_region("dev-someone-1") == "dev-someone-1"
    for bad in ("us_1", "us1.", "-us1", ""):
        with pytest.raises(InvalidRelayRecordError):
            validate_relay_region(bad)


def test_validate_tunnel_endpoint_accepts_host_port_and_rejects_others() -> None:
    assert validate_tunnel_endpoint("198.51.100.1:7000") == "198.51.100.1:7000"
    assert validate_tunnel_endpoint("relay-us1.infra.example.com:7000") == "relay-us1.infra.example.com:7000"
    for bad in ("no-port", "host:", ":7000", "http://host:7000", "host:port", "host:0", "host:99999"):
        with pytest.raises(InvalidRelayRecordError):
            validate_tunnel_endpoint(bad)


def test_validate_relay_ip_address_accepts_ipv4_literals_only() -> None:
    # The registered IP becomes a DNS A-record answer, so only a literal IPv4 works.
    assert validate_relay_ip_address(" 198.51.100.7 ") == "198.51.100.7"
    for bad in ("relay-us1.example.com", "2001:db8::1", "198.51.100", ""):
        with pytest.raises(InvalidRelayRecordError):
            validate_relay_ip_address(bad)


def test_eligible_regions_covers_active_relays_only() -> None:
    rows = [
        make_relay_row("relay-" + "a" * 8, region="us2"),
        make_relay_row("relay-" + "b" * 8, region="us1"),
        make_relay_row("relay-" + "c" * 8, region="eu1", is_active=False),
    ]
    assert eligible_regions(rows) == ["us1", "us2"]


def test_pick_fallback_region_is_deterministic_and_order_insensitive() -> None:
    host_id = "host-" + "9" * 32
    picked = pick_fallback_region(host_id, ["us1", "us2"])
    assert picked in ("us1", "us2")
    assert pick_fallback_region(host_id, ["us2", "us1"]) == picked
    assert pick_fallback_region(host_id, ["us1"]) == "us1"
    # An empty region list violates the documented precondition and must fail
    # with the clear no-active-relay error, not an arithmetic error.
    with pytest.raises(NoActiveRelaysError):
        pick_fallback_region(host_id, [])


def test_pick_fallback_region_spreads_over_regions() -> None:
    regions = ["us1", "us2"]
    picks = {pick_fallback_region(f"host-{idx:032x}", regions) for idx in range(16)}
    assert picks == {"us1", "us2"}


def test_relay_endpoints_for_region_filters_sorts_and_ignores_health() -> None:
    rows = [
        make_relay_row("relay-" + "b" * 8, region="us1", tunnel_endpoint="10.0.0.2:7000", health="unhealthy"),
        make_relay_row("relay-" + "a" * 8, region="us1", tunnel_endpoint="10.0.0.1:7000"),
        make_relay_row("relay-" + "c" * 8, region="us2", tunnel_endpoint="10.0.0.3:7000"),
        make_relay_row("relay-" + "d" * 8, region="us1", tunnel_endpoint="10.0.0.4:7000", is_active=False),
    ]
    # Unhealthy relays stay in the assignment (health only filters DNS);
    # retired ones and other regions do not.
    assert relay_endpoints_for_region(rows, "us1") == [
        {"relay_id": "relay-" + "a" * 8, "endpoint": "10.0.0.1:7000"},
        {"relay_id": "relay-" + "b" * 8, "endpoint": "10.0.0.2:7000"},
    ]
    assert relay_endpoints_for_region(rows, "eu1") == []


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------


def test_admin_relays_requires_the_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY)

    assert client.get("/admin/relays").status_code == 401
    assert client.get("/admin/relays", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/admin/relays", headers=_admin_headers()).status_code == 200


def test_admin_relays_register_list_and_retire(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY)

    created = client.post(
        "/admin/relays",
        json={
            "region": "us1",
            "tunnel_endpoint": "198.51.100.7:7000",
            "ip_address": "198.51.100.7",
            "instance_name": "share-relay-test-us1-3",
        },
        headers=_admin_headers(),
    )
    assert created.status_code == 200
    created_body = created.json()
    relay_id = created_body["relay_id"]
    assert created_body["region"] == "us1"
    assert created_body["is_active"] is True
    assert created_body["health"] == "healthy"

    listed = client.get("/admin/relays", headers=_admin_headers()).json()["relays"]
    assert relay_id in [row["relay_id"] for row in listed]

    retired = client.delete(f"/admin/relays/{relay_id}", headers=_admin_headers())
    assert retired.status_code == 200
    assert retired.json() == {"relay_id": relay_id, "is_active": False}
    retired_row = next(row for row in backend.relay_rows if row["relay_id"] == relay_id)
    assert retired_row["is_active"] is False


def test_admin_relays_reregister_revives_a_retired_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY)
    client.delete(f"/admin/relays/{_RELAY_ID_US1}", headers=_admin_headers())

    revived = client.post(
        "/admin/relays",
        json={
            "relay_id": _RELAY_ID_US1,
            "region": "us1",
            "tunnel_endpoint": _RELAY_ENDPOINT_US1,
            "ip_address": "198.51.100.1",
        },
        headers=_admin_headers(),
    )

    assert revived.status_code == 200
    assert revived.json()["is_active"] is True
    row = next(row for row in backend.relay_rows if row["relay_id"] == _RELAY_ID_US1)
    assert row["is_active"] is True
    assert row["health"] == "healthy"


def test_admin_relays_rejects_malformed_registrations(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY)

    # Field validators fire inside request-body validation, so FastAPI reports
    # them as 422s (same shape as the share create's entry_label validation).
    bad_region = client.post(
        "/admin/relays",
        json={"region": "US 1!", "tunnel_endpoint": "1.2.3.4:7000", "ip_address": "1.2.3.4"},
        headers=_admin_headers(),
    )
    assert bad_region.status_code == 422
    bad_endpoint = client.post(
        "/admin/relays",
        json={"region": "us1", "tunnel_endpoint": "nope", "ip_address": "1.2.3.4"},
        headers=_admin_headers(),
    )
    assert bad_endpoint.status_code == 422
    bad_ip = client.post(
        "/admin/relays",
        json={"region": "us1", "tunnel_endpoint": "1.2.3.4:7000", "ip_address": "relay-us1.example.com"},
        headers=_admin_headers(),
    )
    assert bad_ip.status_code == 422


def test_admin_relays_retire_404s_for_an_unknown_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY)

    resp = client.delete(f"/admin/relays/{_RELAY_ID_US2.replace('2', '9')}", headers=_admin_headers())

    assert resp.status_code == 404
