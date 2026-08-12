"""Tests for the self-hosted sharing model + endpoints (shares, relay tokens, frps plugin auth)."""

import re
from uuid import uuid4

import pytest

from imbue.remote_service_connector.errors import InvalidShareCoordinateError
from imbue.remote_service_connector.errors import MissingShareConfigError
from imbue.remote_service_connector.errors import ShareQuotaExceededError
from imbue.remote_service_connector.shares import DEFAULT_MAX_SHARED_WORKSPACES_PER_USER
from imbue.remote_service_connector.shares import check_share_quota
from imbue.remote_service_connector.shares import decide_frps_new_proxy
from imbue.remote_service_connector.shares import derive_share_user_label
from imbue.remote_service_connector.shares import generate_relay_token
from imbue.remote_service_connector.shares import hash_relay_token
from imbue.remote_service_connector.shares import make_share_coordinate
from imbue.remote_service_connector.shares import parse_relay_endpoint_map
from imbue.remote_service_connector.shares import resolve_share_region
from imbue.remote_service_connector.testing import _CONTENT_DOMAIN
from imbue.remote_service_connector.testing import _FRPS_SECRET
from imbue.remote_service_connector.testing import _OTHER_HOST_ID
from imbue.remote_service_connector.testing import _RELAY_ENDPOINTS
from imbue.remote_service_connector.testing import _SHARE_STUB_HOST_ID
from imbue.remote_service_connector.testing import _SHARE_STUB_USER_ID
from imbue.remote_service_connector.testing import _SHARE_STUB_USER_LABEL
from imbue.remote_service_connector.testing import _make_share_test_client
from imbue.remote_service_connector.testing import _share_headers

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


def test_parse_relay_endpoint_map_parses_multiple_regions() -> None:
    parsed = parse_relay_endpoint_map(_RELAY_ENDPOINTS)
    assert parsed == {
        "us1": "relay-us1.infra.example.com:7000",
        "us2": "relay-us2.infra.example.com:7000",
    }


@pytest.mark.parametrize("raw", ["", "us1", "=relay:7000", "us1=", ",,,"])
def test_parse_relay_endpoint_map_rejects_malformed_entries(raw: str) -> None:
    with pytest.raises(MissingShareConfigError):
        parse_relay_endpoint_map(raw)


def test_resolve_share_region_maps_datacenters_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHARE_RELAY_ENDPOINTS", _RELAY_ENDPOINTS)
    monkeypatch.setenv("SHARE_DEFAULT_REGION", "us1")
    assert resolve_share_region("US-WEST-OR") == "us1"
    assert resolve_share_region("US-EAST-VA") == "us2"
    assert resolve_share_region("EU-WEST-FR") == "us1"
    assert resolve_share_region(None) == "us1"


def test_resolve_share_region_ignores_mapped_region_without_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHARE_RELAY_ENDPOINTS", "dev-someone-1=relay.dev.example.com:7000")
    monkeypatch.setenv("SHARE_DEFAULT_REGION", "dev-someone-1")
    assert resolve_share_region("US-EAST-VA") == "dev-someone-1"


def test_resolve_share_region_requires_default_region_in_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHARE_RELAY_ENDPOINTS", "us2=relay-us2.infra.example.com:7000")
    monkeypatch.setenv("SHARE_DEFAULT_REGION", "us1")
    with pytest.raises(MissingShareConfigError):
        resolve_share_region(None)


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
    assert body["relay_endpoint"] == "relay-us1.infra.example.com:7000"
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
    assert body["relay_endpoint"] == "relay-us2.infra.example.com:7000"


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
    login_resp = client.post(f"/frps/auth/{_FRPS_SECRET}", json=login_body)
    assert login_resp.json()["reject"] is False

    resp = client.get(f"/shares/{_SHARE_STUB_HOST_ID}/status", headers=_share_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "active"
    assert body["workspace_domain"] == created["workspace_domain"]
    assert body["relay_endpoint"] == "relay-us1.infra.example.com:7000"
    assert body["last_tunnel_login_at"] is not None
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


def test_create_share_rejects_a_malformed_entry_label(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    resp = client.post(
        "/shares",
        json={"host_id": _SHARE_STUB_HOST_ID, "entry_label": "not a label."},
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

    resp = client.post("/frps/auth/wrong-secret", json=_login_op("whatever"))

    assert resp.status_code == 401


def test_frps_auth_is_disabled_without_configured_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    monkeypatch.delenv("FRPS_AUTH_SECRET")

    resp = client.post(f"/frps/auth/{_FRPS_SECRET}", json=_login_op("whatever"))

    assert resp.status_code == 403


def test_frps_auth_login_allows_active_share_and_stamps_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()

    resp = client.post(f"/frps/auth/{_FRPS_SECRET}", json=_login_op(created["relay_token"]))

    assert resp.status_code == 200
    assert resp.json() == {"reject": False, "reject_reason": "", "unchange": True}
    share = backend.find_share(_SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL)
    assert share is not None
    assert share["last_tunnel_login_at"] is not None


def test_frps_auth_rejects_unknown_and_missing_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    unknown = client.post(f"/frps/auth/{_FRPS_SECRET}", json=_login_op("not-a-real-token"))
    assert unknown.json()["reject"] is True

    missing = client.post(f"/frps/auth/{_FRPS_SECRET}", json={"op": "Login", "content": {}})
    assert missing.json()["reject"] is True


def test_frps_auth_rejects_token_of_inactive_share(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    client.delete(f"/shares/{_SHARE_STUB_HOST_ID}", headers=_share_headers())

    resp = client.post(f"/frps/auth/{_FRPS_SECRET}", json=_login_op(created["relay_token"]))

    assert resp.json()["reject"] is True


def test_frps_auth_new_proxy_allows_single_labels_under_own_domain_only(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    domain = created["workspace_domain"]

    allowed = client.post(
        f"/frps/auth/{_FRPS_SECRET}",
        json=_new_proxy_op(created["relay_token"], [f"terminal-abcd1234.{domain}", f"auth-x7k9q2w1.{domain}"]),
    )
    assert allowed.json()["reject"] is False

    # The bare domain and the wildcard must not route under the explicit-claim model.
    bare = client.post(f"/frps/auth/{_FRPS_SECRET}", json=_new_proxy_op(created["relay_token"], [domain]))
    assert bare.json()["reject"] is True
    wildcard = client.post(f"/frps/auth/{_FRPS_SECRET}", json=_new_proxy_op(created["relay_token"], [f"*.{domain}"]))
    assert wildcard.json()["reject"] is True

    foreign_domain = f"terminal-abcd1234.{domain.replace(_SHARE_STUB_HOST_ID, _OTHER_HOST_ID)}"
    rejected = client.post(f"/frps/auth/{_FRPS_SECRET}", json=_new_proxy_op(created["relay_token"], [foreign_domain]))
    assert rejected.json()["reject"] is True


def test_frps_auth_allows_unsubscribed_ops_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()

    resp = client.post(
        f"/frps/auth/{_FRPS_SECRET}",
        json={"op": "Ping", "content": {"user": {"metas": {"relay_token": created["relay_token"]}}}},
    )

    assert resp.json() == {"reject": False, "reject_reason": "", "unchange": True}
