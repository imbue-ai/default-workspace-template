"""Tests for the self-hosted sharing model + endpoints (shares, relay tokens, frps plugin auth)."""

import hashlib
import json
import logging
import re
from typing import Any
from uuid import uuid4

import psycopg2
import pytest
from fastapi.testclient import TestClient

from imbue.modal_app_kit.request_logging import RequestLoggingMiddleware
from imbue.remote_service_connector.errors import InvalidShareCoordinateError
from imbue.remote_service_connector.errors import NoActiveRelaysError
from imbue.remote_service_connector.errors import ShareQuotaExceededError
from imbue.remote_service_connector.shares import DEFAULT_MAX_SHARED_WORKSPACES_PER_USER
from imbue.remote_service_connector.shares import FrpsPingMetricsAggregator
from imbue.remote_service_connector.shares import check_share_quota
from imbue.remote_service_connector.shares import decide_frps_new_proxy
from imbue.remote_service_connector.shares import decide_frps_ping
from imbue.remote_service_connector.shares import derive_share_user_label
from imbue.remote_service_connector.shares import entry_label_from_claimed_domains
from imbue.remote_service_connector.shares import generate_relay_token
from imbue.remote_service_connector.shares import hash_relay_token
from imbue.remote_service_connector.shares import make_share_coordinate
from imbue.remote_service_connector.shares import resolve_share_region
from imbue.remote_service_connector.shares import resolve_share_region_for_share
from imbue.remote_service_connector.testing import _CONTENT_DOMAIN
from imbue.remote_service_connector.testing import _FRPS_SECRET
from imbue.remote_service_connector.testing import _OTHER_HOST_ID
from imbue.remote_service_connector.testing import _RELAY_ENDPOINT_US1
from imbue.remote_service_connector.testing import _RELAY_ENDPOINT_US2
from imbue.remote_service_connector.testing import _RELAY_ID_US1
from imbue.remote_service_connector.testing import _RELAY_ID_US2
from imbue.remote_service_connector.testing import _SHARE_STUB_HOST_ID
from imbue.remote_service_connector.testing import _SHARE_STUB_USER_ID
from imbue.remote_service_connector.testing import _SHARE_STUB_USER_LABEL
from imbue.remote_service_connector.testing import _make_share_test_client
from imbue.remote_service_connector.testing import _share_headers
from imbue.remote_service_connector.web import web_app

# ---------------------------------------------------------------------------
# Pure model
# ---------------------------------------------------------------------------


def test_derive_share_user_label_strips_hyphens_from_uuid() -> None:
    assert derive_share_user_label(_SHARE_STUB_USER_ID) == _SHARE_STUB_USER_LABEL


def test_derive_share_user_label_accepts_already_stripped_hex() -> None:
    raw = uuid4().hex
    assert derive_share_user_label(raw) == raw


def test_derive_share_user_label_rejects_non_uuid_ids() -> None:
    with pytest.raises(InvalidShareCoordinateError):
        derive_share_user_label("not-a-uuid")


def test_make_share_coordinate_builds_workspace_domain_from_parts() -> None:
    coordinate = make_share_coordinate(
        host_id=_SHARE_STUB_HOST_ID,
        user_label=_SHARE_STUB_USER_LABEL,
        region="us1",
        content_domain="imbueminds.com",
    )
    assert coordinate.workspace_domain == f"{_SHARE_STUB_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.imbueminds.com"
    assert coordinate.vhost_wildcard == f"*.{coordinate.workspace_domain}"
    assert coordinate.registrable_site == f"{_SHARE_STUB_USER_LABEL}.us1.imbueminds.com"


@pytest.mark.parametrize(
    ("host_id", "user_label", "region", "content_domain"),
    [
        ("host-short", _SHARE_STUB_USER_LABEL, "us1", "imbueminds.com"),
        ("agent-" + "a" * 32, _SHARE_STUB_USER_LABEL, "us1", "imbueminds.com"),
        (_SHARE_STUB_HOST_ID, "12345678-1234-5678-1234-567812345678", "us1", "imbueminds.com"),
        (_SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL, "US1", "imbueminds.com"),
        (_SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL, "us..1", "imbueminds.com"),
        (_SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL, "us1", "imbue_minds.com"),
        (_SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL, "us1", ".imbueminds.com"),
    ],
)
def test_make_share_coordinate_rejects_invalid_parts(
    host_id: str, user_label: str, region: str, content_domain: str
) -> None:
    with pytest.raises(InvalidShareCoordinateError):
        make_share_coordinate(host_id=host_id, user_label=user_label, region=region, content_domain=content_domain)


def test_relay_tokens_are_unique_urlsafe_and_hash_deterministically() -> None:
    token_one = generate_relay_token()
    token_two = generate_relay_token()
    assert token_one != token_two
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token_one) is not None
    assert hash_relay_token(token_one) == hash_relay_token(token_one)
    assert hash_relay_token(token_one) != hash_relay_token(token_two)
    assert len(hash_relay_token(token_one)) == 64


def test_check_share_quota_allows_below_cap_and_rejects_at_cap() -> None:
    check_share_quota(0, DEFAULT_MAX_SHARED_WORKSPACES_PER_USER)
    check_share_quota(DEFAULT_MAX_SHARED_WORKSPACES_PER_USER - 1, DEFAULT_MAX_SHARED_WORKSPACES_PER_USER)
    with pytest.raises(ShareQuotaExceededError):
        check_share_quota(DEFAULT_MAX_SHARED_WORKSPACES_PER_USER, DEFAULT_MAX_SHARED_WORKSPACES_PER_USER)


def test_decide_frps_new_proxy_allows_single_labels_under_the_domain_case_insensitively() -> None:
    domain = f"{_SHARE_STUB_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.imbueminds.com"
    decision = decide_frps_new_proxy(domain, [f"terminal-abcd1234.{domain}".upper(), f"auth-x7k9q2w1.{domain}"])
    assert decision.reject is False
    assert decision.unchange is True


def test_decide_frps_new_proxy_rejects_bare_domain_wildcard_and_deeper_labels() -> None:
    domain = f"{_SHARE_STUB_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.imbueminds.com"
    # The bare domain (CT-visible cert name) and the wildcard must not route.
    assert decide_frps_new_proxy(domain, [domain]).reject is True
    assert decide_frps_new_proxy(domain, [f"*.{domain}"]).reject is True
    # Deeper (two-label) origins are not single labels under the domain.
    assert decide_frps_new_proxy(domain, [f"a.terminal-abcd1234.{domain}"]).reject is True


def test_decide_frps_new_proxy_rejects_foreign_domains_and_empty_claims() -> None:
    domain = f"{_SHARE_STUB_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.imbueminds.com"
    foreign = f"terminal-abcd1234.{_OTHER_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.imbueminds.com"
    good = f"terminal-abcd1234.{domain}"
    assert decide_frps_new_proxy(domain, [foreign]).reject is True
    assert decide_frps_new_proxy(domain, [good, foreign]).reject is True
    assert decide_frps_new_proxy(domain, []).reject is True


def test_decide_frps_new_proxy_rejects_subdomain_claims() -> None:
    # The relay never enables subdomain routing; rejecting the claim here keeps
    # that guarantee independent of the relay's rendered frps config.
    domain = f"{_SHARE_STUB_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.imbueminds.com"
    assert decide_frps_new_proxy(domain, [f"auth-x7k9q2w1.{domain}"], claimed_subdomain="evil").reject is True


def test_entry_label_from_claimed_domains_picks_the_shell_label() -> None:
    domain = f"{_SHARE_STUB_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.imbueminds.com"
    claims = [f"terminal-abcd1234.{domain}", f"system_interface-elm7wydc.{domain}", f"auth-x7k9q2w1.{domain}"]
    assert entry_label_from_claimed_domains(domain, claims) == "system_interface-elm7wydc"
    # Legacy shell labels predate the random-suffix scheme.
    assert entry_label_from_claimed_domains(domain, [f"system_interface.{domain}"]) == "system_interface"
    # Case-insensitive, like the NewProxy authorization itself.
    assert (
        entry_label_from_claimed_domains(domain, [f"SYSTEM_INTERFACE-ELM7WYDC.{domain}".upper()])
        == "system_interface-elm7wydc"
    )


def test_entry_label_from_claimed_domains_ignores_non_shell_and_foreign_claims() -> None:
    domain = f"{_SHARE_STUB_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.imbueminds.com"
    foreign = f"system_interface-elm7wydc.{_OTHER_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.imbueminds.com"
    assert entry_label_from_claimed_domains(domain, []) is None
    assert entry_label_from_claimed_domains(domain, [f"terminal-abcd1234.{domain}"]) is None
    assert entry_label_from_claimed_domains(domain, [foreign]) is None
    # A lookalike prefix without the ``-`` separator is not the shell service.
    assert entry_label_from_claimed_domains(domain, [f"system_interfacex.{domain}"]) is None


# _SHARE_STUB_HOST_ID (host-aaa...) hash-spreads to us1 and _OTHER_HOST_ID
# (host-bbb...) to us2 under the deterministic fallback; several assertions
# below rely on that stability.
_BOTH_REGIONS = ["us1", "us2"]


def test_resolve_share_region_maps_datacenters_and_falls_back() -> None:
    assert resolve_share_region("US-WEST-OR", _BOTH_REGIONS, _SHARE_STUB_HOST_ID) == "us1"
    assert resolve_share_region("US-EAST-VA", _BOTH_REGIONS, _SHARE_STUB_HOST_ID) == "us2"
    # Unmapped datacenters and latency-unknown hosts spread deterministically.
    assert resolve_share_region("EU-WEST-FR", _BOTH_REGIONS, _SHARE_STUB_HOST_ID) == "us1"
    assert resolve_share_region(None, _BOTH_REGIONS, _SHARE_STUB_HOST_ID) == "us1"
    assert resolve_share_region(None, _BOTH_REGIONS, _OTHER_HOST_ID) == "us2"


def test_resolve_share_region_ignores_mapped_region_without_relay() -> None:
    assert resolve_share_region("US-EAST-VA", ["dev-someone-1"], _SHARE_STUB_HOST_ID) == "dev-someone-1"


def test_resolve_share_region_raises_without_any_eligible_region() -> None:
    with pytest.raises(NoActiveRelaysError):
        resolve_share_region(None, [], _SHARE_STUB_HOST_ID)


def test_resolve_share_region_for_share_is_sticky_on_the_existing_row() -> None:
    # An existing region outranks both the datacenter mapping and a preference.
    assert resolve_share_region_for_share("us2", "US-WEST-OR", None, _BOTH_REGIONS, _SHARE_STUB_HOST_ID) == "us2"
    assert resolve_share_region_for_share("us2", None, "us1", _BOTH_REGIONS, _SHARE_STUB_HOST_ID) == "us2"
    # A recorded region no longer served by any relay fails loudly: the region
    # is baked into the stored domain, so silently answering with another
    # region's relays would leave the persisted share (and the assignment the
    # in-workspace gateway polls) pointing somewhere the response is not.
    with pytest.raises(NoActiveRelaysError):
        resolve_share_region_for_share("eu9", None, None, _BOTH_REGIONS, _SHARE_STUB_HOST_ID)


def test_resolve_share_region_for_share_honors_preference_only_without_a_datacenter() -> None:
    # Local workspaces (no datacenter record) may be steered by the caller.
    assert resolve_share_region_for_share(None, None, "us2", _BOTH_REGIONS, _SHARE_STUB_HOST_ID) == "us2"
    # A pool host's datacenter mapping wins over the caller's preference.
    assert resolve_share_region_for_share(None, "US-WEST-OR", "us2", _BOTH_REGIONS, _SHARE_STUB_HOST_ID) == "us1"
    # An unknown preference is ignored, never an error.
    assert resolve_share_region_for_share(None, None, "eu9", _BOTH_REGIONS, _SHARE_STUB_HOST_ID) == "us1"
    assert resolve_share_region_for_share(None, None, None, _BOTH_REGIONS, _SHARE_STUB_HOST_ID) == "us1"


# ---------------------------------------------------------------------------
# Share CRUD endpoints
# ---------------------------------------------------------------------------


def test_create_share_returns_domain_endpoint_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)

    resp = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["workspace_domain"] == f"{_SHARE_STUB_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.{_CONTENT_DOMAIN}"
    assert body["region"] == "us1"
    assert body["relay_endpoints"] == [{"relay_id": _RELAY_ID_US1, "endpoint": _RELAY_ENDPOINT_US1}]
    assert body["relay_token"]
    share = backend.find_share(_SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL)
    assert share is not None
    assert share["state"] == "active"
    assert len(backend.relay_token_rows) == 1
    assert backend.relay_token_rows[0]["token_hash"] == hash_relay_token(body["relay_token"])


def test_create_share_uses_pool_host_datacenter_region(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    backend.add_available_host(host_id=uuid4(), version="1", host_id_str=_SHARE_STUB_HOST_ID, region="US-EAST-VA")

    resp = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["region"] == "us2"
    assert body["relay_endpoints"] == [{"relay_id": _RELAY_ID_US2, "endpoint": _RELAY_ENDPOINT_US2}]


def test_create_share_again_rotates_token_and_keeps_one_row(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)

    first = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    second = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()

    assert first["relay_token"] != second["relay_token"]
    assert len([s for s in backend.share_rows if s["host_id"] == _SHARE_STUB_HOST_ID]) == 1
    assert len(backend.relay_token_rows) == 1
    assert backend.relay_token_rows[0]["token_hash"] == hash_relay_token(second["relay_token"])


def test_create_share_rejects_malformed_host_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    resp = client.post("/shares", json={"host_id": "host-tooshort"}, headers=_share_headers())

    assert resp.status_code == 400


def test_create_share_enforces_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    for idx in range(DEFAULT_MAX_SHARED_WORKSPACES_PER_USER):
        filler_host_id = f"host-{idx:032x}"
        backend.add_share(
            filler_host_id, _SHARE_STUB_USER_LABEL, "us1", f"{filler_host_id}.{_SHARE_STUB_USER_LABEL}.us1.x.com"
        )

    resp = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers())

    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "quota_exceeded"
    assert resp.json()["detail"]["entitlement"] == "max_shared_workspaces"


def test_create_share_at_quota_still_allows_resharing_an_active_share(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    backend.add_share(
        _SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL, "us1", f"{_SHARE_STUB_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.x.com"
    )
    for idx in range(DEFAULT_MAX_SHARED_WORKSPACES_PER_USER - 1):
        filler_host_id = f"host-{idx:032x}"
        backend.add_share(
            filler_host_id, _SHARE_STUB_USER_LABEL, "us1", f"{filler_host_id}.{_SHARE_STUB_USER_LABEL}.us1.x.com"
        )

    resp = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers())

    assert resp.status_code == 200


def test_create_share_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    assert client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}).status_code == 401
    resp = client.post(
        "/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers={"Authorization": "Bearer wrong-token"}
    )
    assert resp.status_code == 401


def test_create_share_returns_503_when_sharing_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    monkeypatch.delenv("SHARE_CONTENT_DOMAIN")

    resp = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers())

    assert resp.status_code == 503


def test_list_shares_returns_only_callers_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    backend.add_share(
        _SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL, "us1", f"{_SHARE_STUB_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.x.com"
    )
    backend.add_share(_OTHER_HOST_ID, uuid4().hex, "us1", f"{_OTHER_HOST_ID}.someoneelse.us1.x.com")

    resp = client.get("/shares", headers=_share_headers())

    assert resp.status_code == 200
    shares = resp.json()["shares"]
    assert [s["host_id"] for s in shares] == [_SHARE_STUB_HOST_ID]


def test_delete_share_deactivates_and_deletes_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    assert created["relay_token"]

    resp = client.delete(f"/shares/{_SHARE_STUB_HOST_ID}", headers=_share_headers())

    assert resp.status_code == 200
    assert resp.json() == {"host_id": _SHARE_STUB_HOST_ID, "state": "inactive"}
    share = backend.find_share(_SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL)
    assert share is not None
    assert share["state"] == "inactive"
    assert backend.relay_token_rows == []


def test_delete_share_404s_for_unknown_host(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    resp = client.delete(f"/shares/{_SHARE_STUB_HOST_ID}", headers=_share_headers())

    assert resp.status_code == 404


def test_share_status_reports_state_endpoint_and_login_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()

    login_body = {"op": "Login", "content": {"metas": {"relay_token": created["relay_token"]}}}
    login_resp = client.post(f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}", json=login_body)
    assert login_resp.json()["reject"] is False

    resp = client.get(f"/shares/{_SHARE_STUB_HOST_ID}/status", headers=_share_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "active"
    assert body["workspace_domain"] == created["workspace_domain"]
    assert body["relay_endpoints"] == [{"relay_id": _RELAY_ID_US1, "endpoint": _RELAY_ENDPOINT_US1}]
    assert body["last_tunnel_login_at"] is not None
    # The per-relay login stamp identifies WHICH relay the tunnel reached.
    assert [entry["relay_id"] for entry in body["relays"]] == [_RELAY_ID_US1]
    assert body["relays"][0]["last_login_at"] is not None
    assert body["cert_not_after"] is None
    assert backend.find_share(_SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL) is not None


def test_create_share_records_and_status_reports_the_entry_label(monkeypatch: pytest.MonkeyPatch) -> None:
    # The desktop's client-side flow supplies the shell label with the create;
    # the status read is where the chrome learns its routable entry origin. A
    # later create WITHOUT a label must keep the recorded one (COALESCE).
    client, _backend = _make_share_test_client(monkeypatch)
    created = client.post(
        "/shares",
        json={"host_id": _SHARE_STUB_HOST_ID, "entry_label": "system_interface-abc123"},
        headers=_share_headers(),
    )
    assert created.status_code == 200

    status = client.get(f"/shares/{_SHARE_STUB_HOST_ID}/status", headers=_share_headers()).json()
    assert status["entry_label"] == "system_interface-abc123"

    recreated = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers())
    assert recreated.status_code == 200
    status_after = client.get(f"/shares/{_SHARE_STUB_HOST_ID}/status", headers=_share_headers()).json()
    assert status_after["entry_label"] == "system_interface-abc123"


def test_create_share_honors_preferred_region_for_local_hosts_and_stays_sticky(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The stub host has no pool row (a local workspace), so the caller's
    # measured preference picks the relay on the first share.
    client, _backend = _make_share_test_client(monkeypatch)

    created = client.post(
        "/shares",
        json={"host_id": _SHARE_STUB_HOST_ID, "preferred_region": "us2"},
        headers=_share_headers(),
    ).json()
    assert created["region"] == "us2"
    assert ".us2." in created["workspace_domain"]
    assert created["relay_endpoints"] == [{"relay_id": _RELAY_ID_US2, "endpoint": _RELAY_ENDPOINT_US2}]

    # A re-share with a different preference keeps the baked region: the
    # domain (DNS, PSL boundary, cert, session cookies) must never silently move.
    recreated = client.post(
        "/shares",
        json={"host_id": _SHARE_STUB_HOST_ID, "preferred_region": "us1"},
        headers=_share_headers(),
    ).json()
    assert recreated["region"] == "us2"
    assert recreated["workspace_domain"] == created["workspace_domain"]


def test_create_share_ignores_an_unknown_preferred_region(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    created = client.post(
        "/shares",
        json={"host_id": _SHARE_STUB_HOST_ID, "preferred_region": "eu9"},
        headers=_share_headers(),
    ).json()

    assert created["region"] == "us1"


def test_create_share_rejects_a_malformed_preferred_region(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    resp = client.post(
        "/shares",
        json={"host_id": _SHARE_STUB_HOST_ID, "preferred_region": "Not A Region!"},
        headers=_share_headers(),
    )

    assert resp.status_code == 422


def test_list_share_relays_returns_the_region_endpoint_map(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    resp = client.get("/shares/relays", headers=_share_headers())

    assert resp.status_code == 200
    assert resp.json() == {
        "relays": {
            "us1": [_RELAY_ENDPOINT_US1],
            "us2": [_RELAY_ENDPOINT_US2],
        }
    }


def test_create_share_returns_every_relay_of_a_multi_relay_region(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    second_relay_id = "relay-" + "3" * 16
    backend.add_relay(second_relay_id, "us1", "relay-us1b.infra.example.com:7000", ip_address="198.51.100.3")

    body = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()

    assert body["region"] == "us1"
    assert body["relay_endpoints"] == [
        {"relay_id": _RELAY_ID_US1, "endpoint": _RELAY_ENDPOINT_US1},
        {"relay_id": second_relay_id, "endpoint": "relay-us1b.infra.example.com:7000"},
    ]


def test_create_share_returns_503_when_no_relay_is_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    backend.relay_rows.clear()

    resp = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers())

    assert resp.status_code == 503


def test_create_share_ignores_retired_relays(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    for relay_row in backend.relay_rows:
        if relay_row["region"] == "us2":
            relay_row["is_active"] = False

    # us2 is no longer eligible, so the datacenter-mapped region falls through
    # to the spread over the remaining region.
    backend.add_available_host(host_id=uuid4(), version="1", host_id_str=_SHARE_STUB_HOST_ID, region="US-EAST-VA")
    body = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()

    assert body["region"] == "us1"
    assert body["relay_endpoints"] == [{"relay_id": _RELAY_ID_US1, "endpoint": _RELAY_ENDPOINT_US1}]


# ---------------------------------------------------------------------------
# Gateway assignment endpoint
# ---------------------------------------------------------------------------


def test_share_assignment_returns_endpoints_for_the_relay_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    second_relay_id = "relay-" + "3" * 16
    backend.add_relay(second_relay_id, "us1", "relay-us1b.infra.example.com:7000", ip_address="198.51.100.3")

    resp = client.get("/shares/assignment", headers={"Authorization": f"Bearer {created['relay_token']}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["workspace_domain"] == created["workspace_domain"]
    assert body["relay_endpoints"] == [
        {"relay_id": _RELAY_ID_US1, "endpoint": _RELAY_ENDPOINT_US1},
        {"relay_id": second_relay_id, "endpoint": "relay-us1b.infra.example.com:7000"},
    ]
    assert body["poll_seconds"] > 0


def test_share_assignment_rejects_missing_unknown_and_revoked_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()

    assert client.get("/shares/assignment").status_code == 401
    assert client.get("/shares/assignment", headers={"Authorization": "Bearer nope"}).status_code == 401

    client.delete(f"/shares/{_SHARE_STUB_HOST_ID}", headers=_share_headers())
    revoked = client.get("/shares/assignment", headers={"Authorization": f"Bearer {created['relay_token']}"})
    assert revoked.status_code == 401


def test_create_share_rejects_a_malformed_entry_label(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    resp = client.post(
        "/shares",
        json={"host_id": _SHARE_STUB_HOST_ID, "entry_label": "not a label."},
        headers=_share_headers(),
    )

    assert resp.status_code == 422


def test_create_share_rejects_a_malformed_workspace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    for bad_workspace_id in ("not-an-agent-id", ""):
        resp = client.post(
            "/shares",
            json={"host_id": _SHARE_STUB_HOST_ID, "workspace_id": bad_workspace_id},
            headers=_share_headers(),
        )

        assert resp.status_code == 422


def test_share_status_404s_for_unknown_host(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    resp = client.get(f"/shares/{_SHARE_STUB_HOST_ID}/status", headers=_share_headers())

    assert resp.status_code == 404


def test_share_status_reports_cert_expiry_when_issued(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    backend.issued_cert_rows.append(
        {"workspace_domain": created["workspace_domain"], "not_after": "2026-10-01T00:00:00+00:00"}
    )
    backend.issued_cert_rows.append(
        {"workspace_domain": created["workspace_domain"], "not_after": "2026-09-01T00:00:00+00:00"}
    )

    resp = client.get(f"/shares/{_SHARE_STUB_HOST_ID}/status", headers=_share_headers())

    assert resp.json()["cert_not_after"] == "2026-10-01T00:00:00+00:00"


def test_share_status_reports_empty_endpoints_when_the_region_lost_its_relays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Status is a read: a share whose region has no active relay left must
    # still answer (with an empty endpoint list) rather than 503 -- only
    # share bring-up hard-fails on a relay-less region.
    client, backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    assert created["region"] == "us1"
    for relay_row in backend.relay_rows:
        if relay_row["region"] == "us1":
            relay_row["is_active"] = False

    resp = client.get(f"/shares/{_SHARE_STUB_HOST_ID}/status", headers=_share_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "active"
    assert body["relay_endpoints"] == []


# ---------------------------------------------------------------------------
# frps plugin auth endpoint
# ---------------------------------------------------------------------------


def _login_op(relay_token: str) -> dict[str, object]:
    return {"op": "Login", "content": {"metas": {"relay_token": relay_token}}}


def _new_proxy_op(relay_token: str, custom_domains: list[str]) -> dict[str, object]:
    return {
        "op": "NewProxy",
        "content": {
            "user": {"user": "workspace", "metas": {"relay_token": relay_token}},
            "proxy_name": "share",
            "proxy_type": "https",
            "custom_domains": custom_domains,
        },
    }


def test_frps_auth_rejects_wrong_plugin_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    resp = client.post(f"/frps/auth/wrong-secret/{_RELAY_ID_US1}", json=_login_op("whatever"))

    assert resp.status_code == 401


def test_frps_auth_is_disabled_without_configured_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    monkeypatch.delenv("FRPS_AUTH_SECRET")

    resp = client.post(f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}", json=_login_op("whatever"))

    assert resp.status_code == 403


def test_frps_auth_login_allows_active_share_and_stamps_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()

    resp = client.post(f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}", json=_login_op(created["relay_token"]))

    assert resp.status_code == 200
    assert resp.json() == {"reject": False, "reject_reason": "", "unchange": True}
    share = backend.find_share(_SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL)
    assert share is not None
    assert share["last_tunnel_login_at"] is not None


def test_frps_auth_rejects_unknown_and_missing_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    unknown = client.post(f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}", json=_login_op("not-a-real-token"))
    assert unknown.json()["reject"] is True

    missing = client.post(f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}", json={"op": "Login", "content": {}})
    assert missing.json()["reject"] is True


def test_frps_auth_rejects_token_of_inactive_share(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    client.delete(f"/shares/{_SHARE_STUB_HOST_ID}", headers=_share_headers())

    resp = client.post(f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}", json=_login_op(created["relay_token"]))

    assert resp.json()["reject"] is True


def test_frps_auth_rejects_unknown_and_retired_relay_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    # A retired relay leaves frps auth (not just assignment and DNS): even a
    # valid relay token must be rejected when the path's relay id has no
    # active row.
    client, backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()

    unknown_relay_id = "relay-" + "9" * 16
    unknown = client.post(f"/frps/auth/{_FRPS_SECRET}/{unknown_relay_id}", json=_login_op(created["relay_token"]))
    assert unknown.json()["reject"] is True

    for relay_row in backend.relay_rows:
        if relay_row["relay_id"] == _RELAY_ID_US1:
            relay_row["is_active"] = False
    retired = client.post(f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}", json=_login_op(created["relay_token"]))
    assert retired.json()["reject"] is True


def test_frps_auth_new_proxy_records_the_shell_entry_label(monkeypatch: pytest.MonkeyPatch) -> None:
    # The tunnel's hostname claim is where the connector learns the chrome's
    # entry origin (it never reads anything from inside the workspace).
    client, backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    domain = created["workspace_domain"]

    resp = client.post(
        f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}",
        json=_new_proxy_op(
            created["relay_token"],
            [f"terminal-abcd1234.{domain}", f"system_interface-elm7wydc.{domain}", f"auth-x7k9q2w1.{domain}"],
        ),
    )

    assert resp.json()["reject"] is False
    share = backend.find_share(_SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL)
    assert share is not None
    assert share["entry_label"] == "system_interface-elm7wydc"
    status = client.get(f"/shares/{_SHARE_STUB_HOST_ID}/status", headers=_share_headers()).json()
    assert status["entry_label"] == "system_interface-elm7wydc"


def test_frps_auth_rejected_new_proxy_records_no_entry_label(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    domain = created["workspace_domain"]
    foreign = "system_interface-elm7wydc.host-" + "b" * 32 + ".x.us1.example.com"

    resp = client.post(
        f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}",
        json=_new_proxy_op(created["relay_token"], [f"system_interface-elm7wydc.{domain}", foreign]),
    )

    assert resp.json()["reject"] is True
    share = backend.find_share(_SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL)
    assert share is not None
    assert share["entry_label"] is None


def test_frps_auth_new_proxy_allows_single_labels_under_own_domain_only(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    domain = created["workspace_domain"]

    allowed = client.post(
        f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}",
        json=_new_proxy_op(created["relay_token"], [f"terminal-abcd1234.{domain}", f"auth-x7k9q2w1.{domain}"]),
    )
    assert allowed.json()["reject"] is False

    # The bare domain and the wildcard must not route under the explicit-claim model.
    bare = client.post(
        f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}", json=_new_proxy_op(created["relay_token"], [domain])
    )
    assert bare.json()["reject"] is True
    wildcard = client.post(
        f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}", json=_new_proxy_op(created["relay_token"], [f"*.{domain}"])
    )
    assert wildcard.json()["reject"] is True

    foreign_domain = f"terminal-abcd1234.{domain.replace(_SHARE_STUB_HOST_ID, _OTHER_HOST_ID)}"
    rejected = client.post(
        f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}", json=_new_proxy_op(created["relay_token"], [foreign_domain])
    )
    assert rejected.json()["reject"] is True


def test_frps_auth_allows_unsubscribed_ops_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()

    resp = client.post(
        f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}",
        json={"op": "NewWorkConn", "content": {"user": {"metas": {"relay_token": created["relay_token"]}}}},
    )

    assert resp.json() == {"reject": False, "reject_reason": "", "unchange": True}


def test_frps_auth_allows_ping_for_active_share(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()

    resp = client.post(
        f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}",
        json={"op": "Ping", "content": {"user": {"metas": {"relay_token": created["relay_token"]}}}},
    )

    assert resp.json() == {"reject": False, "reject_reason": "", "unchange": True}


def test_frps_auth_rejects_ping_once_share_is_suspended(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live-tunnel kill switch: a suspended share's next heartbeat is refused."""
    client, backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    ping_body = {"op": "Ping", "content": {"user": {"metas": {"relay_token": created["relay_token"]}}}}
    assert client.post(f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}", json=ping_body).json()["reject"] is False

    for share in backend.share_rows:
        share["state"] = "suspended"

    rejected = client.post(f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}", json=ping_body)
    assert rejected.json()["reject"] is True


def test_frps_auth_rejects_ping_after_unshare(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal unshare also severs the live tunnel: the deleted token no longer resolves."""
    client, _backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    client.delete(f"/shares/{_SHARE_STUB_HOST_ID}", headers=_share_headers())

    rejected = client.post(
        f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}",
        json={"op": "Ping", "content": {"user": {"metas": {"relay_token": created["relay_token"]}}}},
    )

    assert rejected.json()["reject"] is True


def test_decide_frps_ping_fails_open_on_lookup_error() -> None:
    """A connector-internal failure must not kill every live tunnel (frp fails closed on errors)."""

    def _broken_lookup(token_hash: str) -> dict[str, Any] | None:
        raise psycopg2.OperationalError("simulated db outage 71634")

    decision = decide_frps_ping(_broken_lookup, "some-relay-token")

    assert decision.reject is False


def test_decide_frps_ping_rejects_missing_token() -> None:
    decision = decide_frps_ping(lambda token_hash: None, None)

    assert decision.reject is True


class _CountingShareLookup:
    """Share lookup returning a fixed row while counting invocations."""

    def __init__(self, share: dict[str, Any] | None) -> None:
        self.share = share
        self.call_count = 0

    def __call__(self, token_hash: str) -> dict[str, Any] | None:
        self.call_count += 1
        return self.share


def test_decide_frps_ping_serves_allows_from_cache_within_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_FRPS_PING_CACHE_TTL_SECONDS", "30")
    lookup = _CountingShareLookup({"state": "active"})
    token = uuid4().hex
    clock = [1000.0]

    first = decide_frps_ping(lookup, token, monotonic=lambda: clock[0])
    clock[0] += 29.0
    second = decide_frps_ping(lookup, token, monotonic=lambda: clock[0])

    assert first.reject is False
    assert second.reject is False
    assert lookup.call_count == 1


def test_decide_frps_ping_reconsults_the_db_after_the_cached_allow_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kill-switch bound: a state flip is seen on the first ping after the TTL."""
    monkeypatch.setenv("MINDS_FRPS_PING_CACHE_TTL_SECONDS", "30")
    lookup = _CountingShareLookup({"state": "active"})
    token = uuid4().hex
    clock = [1000.0]

    decide_frps_ping(lookup, token, monotonic=lambda: clock[0])
    lookup.share = {"state": "suspended"}
    clock[0] += 31.0
    decision = decide_frps_ping(lookup, token, monotonic=lambda: clock[0])

    assert decision.reject is True
    assert lookup.call_count == 2


def test_decide_frps_ping_never_caches_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    """A re-authorized session must not be re-severed by a stale cached reject."""
    monkeypatch.setenv("MINDS_FRPS_PING_CACHE_TTL_SECONDS", "30")
    lookup = _CountingShareLookup(None)
    token = uuid4().hex

    first = decide_frps_ping(lookup, token)
    lookup.share = {"state": "active"}
    second = decide_frps_ping(lookup, token)

    assert first.reject is True
    assert second.reject is False
    assert lookup.call_count == 2


def test_decide_frps_ping_never_caches_fail_open_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_FRPS_PING_CACHE_TTL_SECONDS", "30")
    call_count = [0]

    def _broken_lookup(token_hash: str) -> dict[str, Any] | None:
        call_count[0] += 1
        raise psycopg2.OperationalError("simulated db outage 52917")

    token = uuid4().hex
    first = decide_frps_ping(_broken_lookup, token)
    second = decide_frps_ping(_broken_lookup, token)

    assert first.reject is False
    assert second.reject is False
    assert call_count[0] == 2


def test_decide_frps_ping_hits_the_db_every_time_when_the_cache_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINDS_FRPS_PING_CACHE_TTL_SECONDS", "0")
    lookup = _CountingShareLookup({"state": "active"})
    token = uuid4().hex

    decide_frps_ping(lookup, token)
    decide_frps_ping(lookup, token)

    assert lookup.call_count == 2


# ---------------------------------------------------------------------------
# Ping metrics aggregation and access-log suppression
# ---------------------------------------------------------------------------


def _make_aggregator(emitted: list[tuple[str, float, dict[str, str]]]) -> FrpsPingMetricsAggregator:
    return FrpsPingMetricsAggregator(
        flush_interval_seconds=60.0,
        emit=lambda name, value, tags: emitted.append((name, value, dict(tags))),
    )


def test_ping_metrics_aggregator_holds_counts_until_the_window_elapses() -> None:
    emitted: list[tuple[str, float, dict[str, str]]] = []
    aggregator = _make_aggregator(emitted)

    aggregator.record_authorized_ping("relay-a1", 12.0, now=1000.0)
    aggregator.record_authorized_ping("relay-a1", 8.0, now=1030.0)
    assert emitted == []

    aggregator.record_authorized_ping("relay-a1", 10.0, now=1060.0)

    assert emitted == [
        ("frps_ping_authorized", 3, {"relay": "relay-a1"}),
        ("frps_ping_authorized_duration_ms_total", 30.0, {"relay": "relay-a1"}),
    ]


def test_ping_metrics_aggregator_emits_one_pair_per_relay() -> None:
    emitted: list[tuple[str, float, dict[str, str]]] = []
    aggregator = _make_aggregator(emitted)

    aggregator.record_authorized_ping("relay-b2", 5.0, now=1000.0)
    aggregator.record_authorized_ping("relay-a1", 7.0, now=1001.0)
    aggregator.flush(now=1002.0)

    assert emitted == [
        ("frps_ping_authorized", 1, {"relay": "relay-a1"}),
        ("frps_ping_authorized_duration_ms_total", 7.0, {"relay": "relay-a1"}),
        ("frps_ping_authorized", 1, {"relay": "relay-b2"}),
        ("frps_ping_authorized_duration_ms_total", 5.0, {"relay": "relay-b2"}),
    ]


def test_ping_metrics_aggregator_resets_between_windows() -> None:
    emitted: list[tuple[str, float, dict[str, str]]] = []
    aggregator = _make_aggregator(emitted)
    aggregator.record_authorized_ping("relay-a1", 4.0, now=1000.0)
    aggregator.flush(now=1001.0)

    aggregator.record_authorized_ping("relay-a1", 6.0, now=1002.0)
    aggregator.flush(now=1003.0)

    assert emitted[2:] == [
        ("frps_ping_authorized", 1, {"relay": "relay-a1"}),
        ("frps_ping_authorized_duration_ms_total", 6.0, {"relay": "relay-a1"}),
    ]


def test_ping_metrics_aggregator_flush_of_an_empty_window_emits_nothing() -> None:
    emitted: list[tuple[str, float, dict[str, str]]] = []
    aggregator = _make_aggregator(emitted)

    aggregator.flush(now=1000.0)

    assert emitted == []


def _make_log_capturing_share_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, Any, list[str]]:
    """A share test client wrapped in the access-log middleware, capturing its lines."""
    inner_client, backend = _make_share_test_client(monkeypatch)
    del inner_client
    lines: list[str] = []
    wrapped = TestClient(RequestLoggingMiddleware(web_app, line_sink=lines.append), raise_server_exceptions=False)
    return wrapped, backend, lines


def test_successful_pings_emit_no_access_log_line(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, lines = _make_log_capturing_share_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    lines.clear()

    resp = client.post(
        f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}",
        json={"op": "Ping", "content": {"user": {"metas": {"relay_token": created["relay_token"]}}}},
    )

    assert resp.json()["reject"] is False
    assert lines == []


def test_rejected_pings_log_a_line_with_the_secret_path_segment_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _backend, lines = _make_log_capturing_share_client(monkeypatch)
    lines.clear()

    resp = client.post(
        f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}",
        json={"op": "Ping", "content": {"user": {"metas": {"relay_token": "not-a-real-token"}}}},
    )

    assert resp.json()["reject"] is True
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["path"] == f"/frps/auth/<plugin-secret>/{_RELAY_ID_US1}"
    assert _FRPS_SECRET not in lines[0]


def test_login_lines_are_logged_with_the_secret_path_segment_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _backend, lines = _make_log_capturing_share_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    lines.clear()

    resp = client.post(f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}", json=_login_op(created["relay_token"]))

    assert resp.json()["reject"] is False
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["path"] == f"/frps/auth/<plugin-secret>/{_RELAY_ID_US1}"
    assert _FRPS_SECRET not in lines[0]


def test_app_shutdown_flushes_buffered_ping_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifespan hook is what makes graceful container scaledown lose no counts."""
    client, _backend, _lines = _make_log_capturing_share_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()

    # The metrics logger installs a dedicated non-propagating handler at
    # import; flip propagate so caplog-style capture sees the flush, then
    # restore (same technique as metrics_test).
    metrics_logger = logging.getLogger("imbue.modal_app_kit.metrics")
    captured: list[str] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _CapturingHandler()
    metrics_logger.addHandler(handler)
    try:
        with TestClient(web_app):
            ping = client.post(
                f"/frps/auth/{_FRPS_SECRET}/{_RELAY_ID_US1}",
                json={"op": "Ping", "content": {"user": {"metas": {"relay_token": created["relay_token"]}}}},
            )
            assert ping.json()["reject"] is False
    finally:
        metrics_logger.removeHandler(handler)

    flushed = [line for line in captured if "frps_ping_authorized" in line]
    assert len(flushed) >= 1
    parsed = json.loads(flushed[0])
    assert parsed["tags"]["relay"] == _RELAY_ID_US1


# ---------------------------------------------------------------------------
# Workspace-keyed shares (minted share labels, hashed user segment)
# ---------------------------------------------------------------------------

_STUB_WORKSPACE_ID = "agent-" + "c" * 32


def test_create_share_with_workspace_id_mints_a_label_led_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)

    resp = client.post(
        "/shares",
        json={"host_id": _SHARE_STUB_HOST_ID, "workspace_id": _STUB_WORKSPACE_ID},
        headers=_share_headers(),
    )

    assert resp.status_code == 200
    body = resp.json()
    labels = str(body["workspace_domain"]).split(".")
    # No internal id appears in the (CT-logged) domain: a random 32-hex share
    # label leads, and the user segment is a one-way hash of the user id.
    assert re.fullmatch(r"[a-f0-9]{32}", labels[0])
    assert labels[0] != _SHARE_STUB_HOST_ID
    assert labels[1] == hashlib.sha256(_SHARE_STUB_USER_ID.encode()).hexdigest()[:32]
    assert labels[1] != _SHARE_STUB_USER_LABEL
    assert body["workspace_id"] == _STUB_WORKSPACE_ID
    share = backend.find_share(_SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL)
    assert share is not None
    assert share["workspace_id"] == _STUB_WORKSPACE_ID
    assert share["share_label"] == labels[0]


def test_reshare_keeps_the_minted_domain_and_rotates_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    body = {"host_id": _SHARE_STUB_HOST_ID, "workspace_id": _STUB_WORKSPACE_ID}

    first = client.post("/shares", json=body, headers=_share_headers()).json()
    client.delete(f"/shares/{_SHARE_STUB_HOST_ID}", headers=_share_headers())
    second = client.post("/shares", json=body, headers=_share_headers()).json()

    # The label is minted once at the workspace's first share and persisted:
    # unshare/re-share resurrects the same URL with a fresh token.
    assert second["workspace_domain"] == first["workspace_domain"]
    assert second["relay_token"] != first["relay_token"]


def test_create_share_never_reuses_another_workspaces_row_on_a_recycled_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    other_workspace_id = "agent-" + "d" * 32
    # Workspace A shared while it ran on this machine; the machine has since
    # been reused by workspace B (the host is a mutable machine attribute).
    first = client.post(
        "/shares",
        json={"host_id": _SHARE_STUB_HOST_ID, "workspace_id": other_workspace_id},
        headers=_share_headers(),
    ).json()

    second = client.post(
        "/shares",
        json={"host_id": _SHARE_STUB_HOST_ID, "workspace_id": _STUB_WORKSPACE_ID},
        headers=_share_headers(),
    ).json()

    # B must never inherit A's identity: its share carries its own workspace
    # id and a freshly minted domain, not A's (whose grants, bookmarks, and
    # cookies all hang off A's domain).
    assert second["workspace_id"] == _STUB_WORKSPACE_ID
    assert second["workspace_domain"] != first["workspace_domain"]


def test_create_share_backfills_workspace_id_onto_a_legacy_row(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    # A share created by an old client (no workspace id): host-led domain.
    legacy = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    assert legacy["workspace_domain"].startswith(f"{_SHARE_STUB_HOST_ID}.")

    # A new client re-shares with the workspace id: the legacy domain is kept
    # (grants/bookmarks/certs hang off it) and the row is backfilled.
    reshared = client.post(
        "/shares",
        json={"host_id": _SHARE_STUB_HOST_ID, "workspace_id": _STUB_WORKSPACE_ID},
        headers=_share_headers(),
    ).json()
    assert reshared["workspace_domain"] == legacy["workspace_domain"]
    assert reshared["workspace_id"] == _STUB_WORKSPACE_ID
    share = backend.find_share(_SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL)
    assert share is not None
    assert share["workspace_id"] == _STUB_WORKSPACE_ID
    # The re-share then resolves through the workspace id even before any
    # machine change: a create naming only the workspace's id finds the row.
    by_workspace = backend.share_rows[-1]
    assert by_workspace["workspace_id"] == _STUB_WORKSPACE_ID
