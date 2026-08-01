"""Tests for the self-hosted sharing model + endpoints (shares, relay tokens, frps plugin auth)."""

import json
import re
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from urllib.parse import parse_qs
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.parse import urlsplit
from uuid import uuid4

import jwt as pyjwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import HTTPException
from jwt import algorithms as jwt_algorithms_rsa
from starlette.testclient import TestClient
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult

import imbue.remote_service_connector.app as app_mod
from imbue.remote_service_connector.app import DEFAULT_MAX_SHARED_WORKSPACES_PER_USER
from imbue.remote_service_connector.app import InvalidCsrError
from imbue.remote_service_connector.app import InvalidShareCoordinateError
from imbue.remote_service_connector.app import MissingShareConfigError
from imbue.remote_service_connector.app import ShareQuotaExceededError
from imbue.remote_service_connector.app import acme_ca_configs_from_env
from imbue.remote_service_connector.app import build_broker_jwks
from imbue.remote_service_connector.app import check_share_quota
from imbue.remote_service_connector.app import decide_frps_new_proxy
from imbue.remote_service_connector.app import derive_share_user_label
from imbue.remote_service_connector.app import extract_cert_chain_metadata
from imbue.remote_service_connector.app import generate_relay_token
from imbue.remote_service_connector.app import hash_relay_token
from imbue.remote_service_connector.app import make_share_coordinate
from imbue.remote_service_connector.app import mint_share_handoff_token
from imbue.remote_service_connector.app import parse_acme_ca_list
from imbue.remote_service_connector.app import parse_relay_endpoint_map
from imbue.remote_service_connector.app import resolve_share_region
from imbue.remote_service_connector.app import validate_share_csr
from imbue.remote_service_connector.app import web_app
from imbue.remote_service_connector.testing import FakePoolBackend
from imbue.remote_service_connector.testing import FakeSuperTokensBackend
from imbue.remote_service_connector.testing import make_fake_pool_backend
from imbue.remote_service_connector.testing import make_fake_supertokens_backend

_STUB_TOKEN = "share-user-stub-jwt"
_STUB_USER_ID = "12345678-1234-5678-1234-567812345678"
_STUB_USER_LABEL = "12345678123456781234567812345678"
_STUB_EMAIL = "sharer@example.com"
_STUB_HOST_ID = "host-" + "a" * 32
_OTHER_HOST_ID = "host-" + "b" * 32
_CONTENT_DOMAIN = "minds-test.example"
_DEFAULT_REGION = "us1"
_RELAY_ENDPOINTS = "us1=relay-us1.infra.example.com:7000,us2=relay-us2.infra.example.com:7000"
_FRPS_SECRET = "frps-plugin-secret-8d1c44"


def _share_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_STUB_TOKEN}"}


def _make_share_test_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakePoolBackend]:
    """TestClient with sharing env config, a stubbed SuperTokens user, and the in-memory DB."""

    def _stub_user_id_from_token(token: str) -> str:
        if token != _STUB_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid token")
        return _STUB_USER_ID

    return _make_share_test_client_with_fakes(
        monkeypatch,
        {
            "_get_user_id_from_access_token": _stub_user_id_from_token,
            "_default_email_getter": lambda user_id: _STUB_EMAIL,
        },
    )


def _make_share_test_client_with_fakes(
    monkeypatch: pytest.MonkeyPatch,
    session_fakes: dict[str, object],
) -> tuple[TestClient, FakePoolBackend]:
    """Shared client setup; ``session_fakes`` supplies the token -> user resolution to install."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://fake-supertokens.example.com")
    monkeypatch.setenv("SHARE_CONTENT_DOMAIN", _CONTENT_DOMAIN)
    monkeypatch.setenv("SHARE_DEFAULT_REGION", _DEFAULT_REGION)
    monkeypatch.setenv("SHARE_RELAY_ENDPOINTS", _RELAY_ENDPOINTS)
    monkeypatch.setenv("FRPS_AUTH_SECRET", _FRPS_SECRET)
    for name, fake_impl in session_fakes.items():
        monkeypatch.setattr(app_mod, name, fake_impl)
    backend = make_fake_pool_backend()
    backend.install_on_app_module(app_mod, monkeypatch)
    return TestClient(web_app), backend


# ---------------------------------------------------------------------------
# Pure model
# ---------------------------------------------------------------------------


def test_derive_share_user_label_strips_hyphens_from_uuid() -> None:
    assert derive_share_user_label(_STUB_USER_ID) == _STUB_USER_LABEL


def test_derive_share_user_label_accepts_already_stripped_hex() -> None:
    raw = uuid4().hex
    assert derive_share_user_label(raw) == raw


def test_derive_share_user_label_rejects_non_uuid_ids() -> None:
    with pytest.raises(InvalidShareCoordinateError):
        derive_share_user_label("not-a-uuid")


def test_make_share_coordinate_builds_workspace_domain_from_parts() -> None:
    coordinate = make_share_coordinate(
        host_id=_STUB_HOST_ID,
        user_label=_STUB_USER_LABEL,
        region="us1",
        content_domain="imbueminds.com",
    )
    assert coordinate.workspace_domain == f"{_STUB_HOST_ID}.{_STUB_USER_LABEL}.us1.imbueminds.com"
    assert coordinate.vhost_wildcard == f"*.{coordinate.workspace_domain}"
    assert coordinate.registrable_site == f"{_STUB_USER_LABEL}.us1.imbueminds.com"


@pytest.mark.parametrize(
    ("host_id", "user_label", "region", "content_domain"),
    [
        ("host-short", _STUB_USER_LABEL, "us1", "imbueminds.com"),
        ("agent-" + "a" * 32, _STUB_USER_LABEL, "us1", "imbueminds.com"),
        (_STUB_HOST_ID, "12345678-1234-5678-1234-567812345678", "us1", "imbueminds.com"),
        (_STUB_HOST_ID, _STUB_USER_LABEL, "US1", "imbueminds.com"),
        (_STUB_HOST_ID, _STUB_USER_LABEL, "us..1", "imbueminds.com"),
        (_STUB_HOST_ID, _STUB_USER_LABEL, "us1", "imbue_minds.com"),
        (_STUB_HOST_ID, _STUB_USER_LABEL, "us1", ".imbueminds.com"),
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
    domain = f"{_STUB_HOST_ID}.{_STUB_USER_LABEL}.us1.imbueminds.com"
    decision = decide_frps_new_proxy(domain, [f"terminal-abcd1234.{domain}".upper(), f"auth-x7k9q2w1.{domain}"])
    assert decision.reject is False
    assert decision.unchange is True


def test_decide_frps_new_proxy_rejects_bare_domain_wildcard_and_deeper_labels() -> None:
    domain = f"{_STUB_HOST_ID}.{_STUB_USER_LABEL}.us1.imbueminds.com"
    # The bare domain (CT-visible cert name) and the wildcard must not route.
    assert decide_frps_new_proxy(domain, [domain]).reject is True
    assert decide_frps_new_proxy(domain, [f"*.{domain}"]).reject is True
    # Deeper (two-label) origins are not single labels under the domain.
    assert decide_frps_new_proxy(domain, [f"a.terminal-abcd1234.{domain}"]).reject is True


def test_decide_frps_new_proxy_rejects_foreign_domains_and_empty_claims() -> None:
    domain = f"{_STUB_HOST_ID}.{_STUB_USER_LABEL}.us1.imbueminds.com"
    foreign = f"terminal-abcd1234.{_OTHER_HOST_ID}.{_STUB_USER_LABEL}.us1.imbueminds.com"
    good = f"terminal-abcd1234.{domain}"
    assert decide_frps_new_proxy(domain, [foreign]).reject is True
    assert decide_frps_new_proxy(domain, [good, foreign]).reject is True
    assert decide_frps_new_proxy(domain, []).reject is True


def test_decide_frps_new_proxy_rejects_subdomain_claims() -> None:
    # The relay never enables subdomain routing; rejecting the claim here keeps
    # that guarantee independent of the relay's rendered frps config.
    domain = f"{_STUB_HOST_ID}.{_STUB_USER_LABEL}.us1.imbueminds.com"
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

    resp = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["workspace_domain"] == f"{_STUB_HOST_ID}.{_STUB_USER_LABEL}.us1.{_CONTENT_DOMAIN}"
    assert body["region"] == "us1"
    assert body["relay_endpoint"] == "relay-us1.infra.example.com:7000"
    assert body["relay_token"]
    share = backend.find_share(_STUB_HOST_ID, _STUB_USER_LABEL)
    assert share is not None
    assert share["state"] == "active"
    assert len(backend.relay_token_rows) == 1
    assert backend.relay_token_rows[0]["token_hash"] == hash_relay_token(body["relay_token"])


def test_create_share_uses_pool_host_datacenter_region(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    backend.add_available_host(host_id=uuid4(), version="1", host_id_str=_STUB_HOST_ID, region="US-EAST-VA")

    resp = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["region"] == "us2"
    assert body["relay_endpoint"] == "relay-us2.infra.example.com:7000"


def test_create_share_again_rotates_token_and_keeps_one_row(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)

    first = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers()).json()
    second = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers()).json()

    assert first["relay_token"] != second["relay_token"]
    assert len([s for s in backend.share_rows if s["host_id"] == _STUB_HOST_ID]) == 1
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
        backend.add_share(filler_host_id, _STUB_USER_LABEL, "us1", f"{filler_host_id}.{_STUB_USER_LABEL}.us1.x.com")

    resp = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers())

    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "quota_exceeded"
    assert resp.json()["detail"]["entitlement"] == "max_shared_workspaces"


def test_create_share_at_quota_still_allows_resharing_an_active_share(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    backend.add_share(_STUB_HOST_ID, _STUB_USER_LABEL, "us1", f"{_STUB_HOST_ID}.{_STUB_USER_LABEL}.us1.x.com")
    for idx in range(DEFAULT_MAX_SHARED_WORKSPACES_PER_USER - 1):
        filler_host_id = f"host-{idx:032x}"
        backend.add_share(filler_host_id, _STUB_USER_LABEL, "us1", f"{filler_host_id}.{_STUB_USER_LABEL}.us1.x.com")

    resp = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers())

    assert resp.status_code == 200


def test_create_share_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    assert client.post("/shares", json={"host_id": _STUB_HOST_ID}).status_code == 401
    resp = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


def test_create_share_returns_503_when_sharing_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    monkeypatch.delenv("SHARE_CONTENT_DOMAIN")

    resp = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers())

    assert resp.status_code == 503


def test_list_shares_returns_only_callers_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    backend.add_share(_STUB_HOST_ID, _STUB_USER_LABEL, "us1", f"{_STUB_HOST_ID}.{_STUB_USER_LABEL}.us1.x.com")
    backend.add_share(_OTHER_HOST_ID, uuid4().hex, "us1", f"{_OTHER_HOST_ID}.someoneelse.us1.x.com")

    resp = client.get("/shares", headers=_share_headers())

    assert resp.status_code == 200
    shares = resp.json()["shares"]
    assert [s["host_id"] for s in shares] == [_STUB_HOST_ID]


def test_delete_share_deactivates_and_deletes_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers()).json()
    assert created["relay_token"]

    resp = client.delete(f"/shares/{_STUB_HOST_ID}", headers=_share_headers())

    assert resp.status_code == 200
    assert resp.json() == {"host_id": _STUB_HOST_ID, "state": "inactive"}
    share = backend.find_share(_STUB_HOST_ID, _STUB_USER_LABEL)
    assert share is not None
    assert share["state"] == "inactive"
    assert backend.relay_token_rows == []


def test_delete_share_404s_for_unknown_host(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    resp = client.delete(f"/shares/{_STUB_HOST_ID}", headers=_share_headers())

    assert resp.status_code == 404


def test_share_status_reports_state_endpoint_and_login_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers()).json()

    login_body = {"op": "Login", "content": {"metas": {"relay_token": created["relay_token"]}}}
    login_resp = client.post(f"/frps/auth/{_FRPS_SECRET}", json=login_body)
    assert login_resp.json()["reject"] is False

    resp = client.get(f"/shares/{_STUB_HOST_ID}/status", headers=_share_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "active"
    assert body["workspace_domain"] == created["workspace_domain"]
    assert body["relay_endpoint"] == "relay-us1.infra.example.com:7000"
    assert body["last_tunnel_login_at"] is not None
    assert body["cert_not_after"] is None
    assert backend.find_share(_STUB_HOST_ID, _STUB_USER_LABEL) is not None


def test_share_status_404s_for_unknown_host(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    resp = client.get(f"/shares/{_STUB_HOST_ID}/status", headers=_share_headers())

    assert resp.status_code == 404


def test_share_status_reports_cert_expiry_when_issued(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers()).json()
    backend.issued_cert_rows.append(
        {"workspace_domain": created["workspace_domain"], "not_after": "2026-10-01T00:00:00+00:00"}
    )
    backend.issued_cert_rows.append(
        {"workspace_domain": created["workspace_domain"], "not_after": "2026-09-01T00:00:00+00:00"}
    )

    resp = client.get(f"/shares/{_STUB_HOST_ID}/status", headers=_share_headers())

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
    created = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers()).json()

    resp = client.post(f"/frps/auth/{_FRPS_SECRET}", json=_login_op(created["relay_token"]))

    assert resp.status_code == 200
    assert resp.json() == {"reject": False, "reject_reason": "", "unchange": True}
    share = backend.find_share(_STUB_HOST_ID, _STUB_USER_LABEL)
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
    created = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers()).json()
    client.delete(f"/shares/{_STUB_HOST_ID}", headers=_share_headers())

    resp = client.post(f"/frps/auth/{_FRPS_SECRET}", json=_login_op(created["relay_token"]))

    assert resp.json()["reject"] is True


def test_frps_auth_new_proxy_allows_single_labels_under_own_domain_only(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers()).json()
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

    foreign_domain = f"terminal-abcd1234.{domain.replace(_STUB_HOST_ID, _OTHER_HOST_ID)}"
    rejected = client.post(f"/frps/auth/{_FRPS_SECRET}", json=_new_proxy_op(created["relay_token"], [foreign_domain]))
    assert rejected.json()["reject"] is True


def test_frps_auth_allows_unsubscribed_ops_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers()).json()

    resp = client.post(
        f"/frps/auth/{_FRPS_SECRET}",
        json={"op": "Ping", "content": {"user": {"metas": {"relay_token": created["relay_token"]}}}},
    )

    assert resp.json() == {"reject": False, "reject_reason": "", "unchange": True}


# ---------------------------------------------------------------------------
# ACME issuance
# ---------------------------------------------------------------------------


def _make_workspace_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _make_share_csr(workspace_domain: str, key: rsa.RSAPrivateKey | None = None, sans: list[str] | None = None) -> str:
    resolved_key = key if key is not None else _make_workspace_key()
    resolved_sans = sans if sans is not None else [workspace_domain, f"*.{workspace_domain}"]
    builder = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([]))
    builder = builder.add_extension(
        x509.SubjectAlternativeName([x509.DNSName(name) for name in resolved_sans]), critical=False
    )
    csr = builder.sign(resolved_key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.PEM).decode("utf-8")


def _make_self_signed_chain(sans: list[str]) -> str:
    key = _make_workspace_key()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "share test leaf")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=45))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in sans]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")


def test_parse_acme_ca_list_preserves_order() -> None:
    parsed = parse_acme_ca_list("letsencrypt=https://le.example/dir, zerossl=https://zs.example/dir")
    assert parsed == [("letsencrypt", "https://le.example/dir"), ("zerossl", "https://zs.example/dir")]


@pytest.mark.parametrize("raw", ["", "letsencrypt", "=https://le.example/dir", "letsencrypt=", ",,"])
def test_parse_acme_ca_list_rejects_malformed(raw: str) -> None:
    with pytest.raises(MissingShareConfigError):
        parse_acme_ca_list(raw)


def test_acme_ca_configs_from_env_attaches_eab_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACME_CA_LIST", "letsencrypt=https://le.example/dir,zerossl=https://zs.example/dir")
    monkeypatch.setenv("ACME_EAB_KID_ZEROSSL", "kid-123")
    monkeypatch.setenv("ACME_EAB_HMAC_ZEROSSL", "hmac-456")

    configs = acme_ca_configs_from_env()

    assert [c.name for c in configs] == ["letsencrypt", "zerossl"]
    assert configs[0].eab_kid is None
    assert configs[1].eab_kid == "kid-123"
    assert configs[1].eab_hmac_key == "hmac-456"


def test_validate_share_csr_accepts_exact_domain_and_wildcard() -> None:
    domain = f"{_STUB_HOST_ID}.{_STUB_USER_LABEL}.us1.{_CONTENT_DOMAIN}"
    validate_share_csr(_make_share_csr(domain), domain)


def test_validate_share_csr_rejects_wrong_or_extra_sans() -> None:
    domain = f"{_STUB_HOST_ID}.{_STUB_USER_LABEL}.us1.{_CONTENT_DOMAIN}"
    with pytest.raises(InvalidCsrError):
        validate_share_csr(_make_share_csr(domain, sans=[domain]), domain)
    with pytest.raises(InvalidCsrError):
        validate_share_csr(_make_share_csr(domain, sans=[domain, f"*.{domain}", "evil.example.com"]), domain)
    other_domain = domain.replace(_STUB_HOST_ID, _OTHER_HOST_ID)
    with pytest.raises(InvalidCsrError):
        validate_share_csr(_make_share_csr(other_domain), domain)


def test_validate_share_csr_rejects_garbage_and_weak_keys() -> None:
    domain = f"{_STUB_HOST_ID}.{_STUB_USER_LABEL}.us1.{_CONTENT_DOMAIN}"
    with pytest.raises(InvalidCsrError):
        validate_share_csr("not a csr", domain)
    weak_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    with pytest.raises(InvalidCsrError):
        validate_share_csr(_make_share_csr(domain, key=weak_key), domain)


def test_extract_cert_chain_metadata_reads_leaf() -> None:
    domain = f"{_STUB_HOST_ID}.{_STUB_USER_LABEL}.us1.{_CONTENT_DOMAIN}"
    chain = _make_self_signed_chain([domain, f"*.{domain}"])

    not_after, sans = extract_cert_chain_metadata(chain)

    assert sans == [domain, f"*.{domain}"]
    assert datetime.fromisoformat(not_after) > datetime.now(timezone.utc)


def test_issue_share_cert_requires_valid_relay_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)

    no_auth = client.post("/shares/cert", json={"csr_pem": "x"})
    assert no_auth.status_code == 401

    bad_token = client.post(
        "/shares/cert", json={"csr_pem": "x"}, headers={"Authorization": "Bearer not-a-relay-token"}
    )
    assert bad_token.status_code == 401


def test_issue_share_cert_rejects_token_of_inactive_share(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers()).json()
    client.delete(f"/shares/{_STUB_HOST_ID}", headers=_share_headers())

    resp = client.post(
        "/shares/cert", json={"csr_pem": "x"}, headers={"Authorization": f"Bearer {created['relay_token']}"}
    )

    assert resp.status_code == 401


def test_issue_share_cert_rate_limits_per_share_per_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sixth issuance inside a day 429s before any ACME or DNS work happens."""
    client, backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers()).json()
    for _ in range(5):
        backend.issued_cert_rows.append(
            {
                "workspace_domain": created["workspace_domain"],
                "host_id": _STUB_HOST_ID,
                "user_id": _STUB_USER_LABEL,
                "ca_name": "test-ca",
                "cert_chain_pem": "pem",
                "sans": "[]",
                "not_after": "2027-01-01T00:00:00+00:00",
            }
        )

    resp = client.post(
        "/shares/cert",
        json={"csr_pem": _make_share_csr(created["workspace_domain"])},
        headers={"Authorization": f"Bearer {created['relay_token']}"},
    )

    assert resp.status_code == 429
    assert "last 24 hours" in resp.json()["detail"]


def test_issue_share_cert_rejects_csr_with_wrong_names(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers()).json()
    wrong_domain_csr = _make_share_csr("evil.example.com")

    resp = client.post(
        "/shares/cert",
        json={"csr_pem": wrong_domain_csr},
        headers={"Authorization": f"Bearer {created['relay_token']}"},
    )

    assert resp.status_code == 400


def test_issue_share_cert_returns_chain_and_records_it(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_share_test_client(monkeypatch)
    monkeypatch.setenv("ACME_CA_LIST", "letsencrypt=https://le.example/dir")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "fake-cf-token")
    monkeypatch.setenv("CLOUDFLARE_ZONE_ID", "fake-zone-id")
    created = client.post("/shares", json={"host_id": _STUB_HOST_ID}, headers=_share_headers()).json()
    domain = created["workspace_domain"]
    chain = _make_self_signed_chain([domain, f"*.{domain}"])

    def _stub_issue(
        csr_pem: str, dns_ops: object, account_store: object, ca_configs: list[app_mod.AcmeCaConfig]
    ) -> tuple[str, str]:
        assert ca_configs and ca_configs[0].name == "letsencrypt"
        return chain, "letsencrypt"

    monkeypatch.setattr(app_mod, "issue_share_certificate", _stub_issue)

    resp = client.post(
        "/shares/cert",
        json={"csr_pem": _make_share_csr(domain)},
        headers={"Authorization": f"Bearer {created['relay_token']}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["cert_chain_pem"] == chain
    assert body["ca_name"] == "letsencrypt"
    assert body["sans"] == [domain, f"*.{domain}"]
    assert len(backend.issued_cert_rows) == 1
    recorded = backend.issued_cert_rows[0]
    assert recorded["workspace_domain"] == domain
    assert recorded["ca_name"] == "letsencrypt"

    status = client.get(f"/shares/{_STUB_HOST_ID}/status", headers=_share_headers()).json()
    assert status["cert_not_after"] == body["not_after"]


# ---------------------------------------------------------------------------
# Accounts broker
# ---------------------------------------------------------------------------

_TEST_BROKER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_TEST_BROKER_KEY_PEM = _TEST_BROKER_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")


def _make_broker_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakePoolBackend, FakeSuperTokensBackend]:
    supertokens_backend = make_fake_supertokens_backend()

    # Resolve the SSO cookie against the fake backend's sessions (so a session
    # minted by the broker's own login/OAuth flows works end to end), with the
    # legacy _STUB_TOKEN accepted for tests that seed the cookie directly.
    def _resolve_token_user_id(token: str) -> str:
        if token == _STUB_TOKEN:
            return _STUB_USER_ID
        session = supertokens_backend.sessions_by_access_token.get(token)
        if session is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return session.user_id

    def _resolve_verified_email(user_id: str) -> str | None:
        account = supertokens_backend.accounts_by_id.get(user_id)
        if account is None:
            return _STUB_EMAIL
        return account.email if account.is_verified else None

    _plain_client, backend = _make_share_test_client_with_fakes(
        monkeypatch,
        {
            "_get_user_id_from_access_token": _resolve_token_user_id,
            "_default_email_getter": _resolve_verified_email,
        },
    )
    # The broker's cookies are Secure, so the client must speak https or its
    # cookie jar will store them and then silently refuse to send them back.
    client = TestClient(web_app, base_url="https://testserver")
    monkeypatch.setenv("BROKER_JWT_SIGNING_KEY_PEM", _TEST_BROKER_KEY_PEM)
    supertokens_backend.install_on_app_module(app_mod, monkeypatch)
    return client, backend, supertokens_backend


def _seed_active_share(backend: FakePoolBackend) -> str:
    domain = f"{_STUB_HOST_ID}.{_STUB_USER_LABEL}.us1.{_CONTENT_DOMAIN}"
    backend.add_share(_STUB_HOST_ID, _STUB_USER_LABEL, "us1", domain)
    return domain


def test_build_broker_jwks_matches_signing_key() -> None:
    jwks = build_broker_jwks(_TEST_BROKER_KEY.public_key())

    assert len(jwks["keys"]) == 1
    key_entry = jwks["keys"][0]
    assert key_entry["kty"] == "RSA"
    assert key_entry["alg"] == "RS256"
    assert key_entry["kid"]
    assert "=" not in key_entry["n"]
    reconstructed = jwt_algorithms_rsa.RSAAlgorithm.from_jwk(json.dumps(key_entry))
    assert isinstance(reconstructed, rsa.RSAPublicKey)
    assert reconstructed.public_numbers() == _TEST_BROKER_KEY.public_key().public_numbers()


def test_mint_share_handoff_token_roundtrips_with_jwks() -> None:
    domain = f"{_STUB_HOST_ID}.{_STUB_USER_LABEL}.us1.{_CONTENT_DOMAIN}"

    token = mint_share_handoff_token(
        signing_key=_TEST_BROKER_KEY,
        user_id=_STUB_USER_ID,
        email=_STUB_EMAIL,
        machine_domain=domain,
        nonce="nonce-123",
    )

    claims = pyjwt.decode(token, _TEST_BROKER_KEY.public_key(), algorithms=["RS256"], audience=domain)
    assert claims["sub"] == _STUB_USER_ID
    assert claims["email"] == _STUB_EMAIL
    assert claims["nonce"] == "nonce-123"
    assert claims["jti"]
    assert claims["exp"] - claims["iat"] == 60
    header = pyjwt.get_unverified_header(token)
    assert header["kid"] == build_broker_jwks(_TEST_BROKER_KEY.public_key())["keys"][0]["kid"]


def test_broker_jwks_endpoint_serves_public_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _st = _make_broker_test_client(monkeypatch)

    resp = client.get("/share/jwks.json")

    assert resp.status_code == 200
    assert resp.json() == build_broker_jwks(_TEST_BROKER_KEY.public_key())


def test_broker_authorize_redirects_to_login_without_session(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, _st = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"

    resp = client.get(
        f"/share/authorize?machine_domain={domain}&next=https://web-1a2b3c4d.{domain}/panel"
        f"&callback_origin={callback_origin}&state=abc",
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/share/login?next=")
    # The callback origin (and machine domain) must survive the login round-trip.
    assert "machine_domain" in resp.headers["location"]
    assert "callback_origin" in resp.headers["location"]


def test_broker_authorize_requires_machine_domain_and_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _st = _make_broker_test_client(monkeypatch)

    assert client.get("/share/authorize?state=abc", follow_redirects=False).status_code == 400
    assert client.get("/share/authorize?machine_domain=x.example", follow_redirects=False).status_code == 400


def test_broker_authorize_rejects_missing_or_foreign_callback_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, _st = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    client.cookies.set("imbue_sso_session", _STUB_TOKEN)

    # No callback_origin at all.
    missing = client.get(f"/share/authorize?machine_domain={domain}&state=abc", follow_redirects=False)
    assert missing.status_code == 400
    # A callback_origin on a foreign host would leak a signed token off-domain.
    foreign = client.get(
        f"/share/authorize?machine_domain={domain}&callback_origin=https://auth-x.evil.example.com&state=abc",
        follow_redirects=False,
    )
    assert foreign.status_code == 400
    # The bare domain does not route and is not a valid callback origin.
    bare = client.get(
        f"/share/authorize?machine_domain={domain}&callback_origin=https://{domain}&state=abc",
        follow_redirects=False,
    )
    assert bare.status_code == 400
    # A deeper host is not a single label under the domain: the relay refuses
    # to route it and the wildcard cert does not cover it (mirrors NewProxy).
    deeper = client.get(
        f"/share/authorize?machine_domain={domain}&callback_origin=https://a.auth-x7k9q2w1.{domain}&state=abc",
        follow_redirects=False,
    )
    assert deeper.status_code == 400


def test_broker_authorize_404s_without_active_share(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _st = _make_broker_test_client(monkeypatch)
    client.cookies.set("imbue_sso_session", _STUB_TOKEN)

    resp = client.get(
        "/share/authorize?machine_domain=unknown.example.com"
        "&callback_origin=https://auth-x7k9q2w1.unknown.example.com&state=abc",
        follow_redirects=False,
    )

    assert resp.status_code == 404


def test_broker_authorize_hands_off_signed_token_to_the_auth_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, _st = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    client.cookies.set("imbue_sso_session", _STUB_TOKEN)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"
    next_url = f"https://web-1a2b3c4d.{domain}/panel?x=1"

    resp = client.get(
        f"/share/authorize?machine_domain={domain}&next={next_url}&callback_origin={callback_origin}&state=nonce-9",
        follow_redirects=False,
    )

    assert resp.status_code == 302
    location = resp.headers["location"]
    # Delivered to the dedicated auth origin, not the bare domain.
    assert location.startswith(f"{callback_origin}/_auth/callback?")
    query = parse_qs(urlsplit(location).query)
    assert query["state"] == ["nonce-9"]
    assert query["next"] == [next_url]
    claims = pyjwt.decode(query["token"][0], _TEST_BROKER_KEY.public_key(), algorithms=["RS256"], audience=domain)
    assert claims["sub"] == _STUB_USER_ID
    assert claims["email"] == _STUB_EMAIL
    assert claims["nonce"] == "nonce-9"


def test_broker_authorize_drops_a_foreign_next(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, _st = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    client.cookies.set("imbue_sso_session", _STUB_TOKEN)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"

    resp = client.get(
        f"/share/authorize?machine_domain={domain}&next=https://evil.example.com/"
        f"&callback_origin={callback_origin}&state=nonce-9",
        follow_redirects=False,
    )

    assert resp.status_code == 302
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    # A foreign next is dropped (the gateway falls back to a safe landing spot).
    assert query.get("next", [""]) == [""]


def test_broker_authorize_rejects_inactive_share(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, _st = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    share = backend.find_share(_STUB_HOST_ID, _STUB_USER_LABEL)
    assert share is not None
    share["state"] = "inactive"
    client.cookies.set("imbue_sso_session", _STUB_TOKEN)

    resp = client.get(
        f"/share/authorize?machine_domain={domain}&callback_origin=https://auth-x7k9q2w1.{domain}&state=abc",
        follow_redirects=False,
    )

    assert resp.status_code == 404


def test_broker_login_page_renders_form(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _st = _make_broker_test_client(monkeypatch)

    resp = client.get("/share/login?next=/share/authorize%3Fmachine_domain%3Dx")

    assert resp.status_code == 200
    assert "<form method='post' action='/share/session'>" in resp.text
    # The shared CSS must be wrapped in a <style> element inside <head>;
    # unwrapped it gets hoisted into <body> and renders as page text.
    assert "<style>body{" in resp.text
    assert "</style></head>" in resp.text


def test_broker_session_rejects_cross_site_form_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A login POST whose Origin names another site is refused (login CSRF needs no cookie)."""
    client, _backend, _st_backend = _make_broker_test_client(monkeypatch)

    resp = client.post(
        "/share/session",
        data={"email": "alice@example.com", "password": "pw-123456", "mode": "signin"},
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )

    assert resp.status_code == 403


def test_broker_session_accepts_a_same_origin_form_post(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    signup = st_backend.sign_up(tenant_id="public", email="carol@example.com", password="pw-123456")
    assert isinstance(signup, EPSignUpOkResult)
    st_backend.mark_email_verified(signup.user.id)

    resp = client.post(
        "/share/session",
        data={"email": "carol@example.com", "password": "pw-123456", "mode": "signin", "next": "/"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert resp.status_code == 303


def test_broker_session_sets_cookie_and_redirects_for_verified_user(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    signup = st_backend.sign_up(tenant_id="public", email="alice@example.com", password="pw-123456")
    assert isinstance(signup, EPSignUpOkResult)
    st_backend.mark_email_verified(signup.user.id)

    resp = client.post(
        "/share/session",
        data={
            "email": "alice@example.com",
            "password": "pw-123456",
            "mode": "signin",
            "next": "/share/authorize%3Fa%3Db",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/share/authorize?a=b"
    set_cookie = resp.headers["set-cookie"]
    assert "imbue_sso_session=" in set_cookie
    assert "HttpOnly" in set_cookie


def test_broker_session_shows_verify_page_for_unverified_user(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)

    resp = client.post(
        "/share/session",
        data={"email": "bob@example.com", "password": "pw-123456", "mode": "signup", "next": "/"},
        follow_redirects=False,
    )

    assert resp.status_code == 200
    assert "Check your inbox" in resp.text
    assert len(st_backend.sent_verification_emails) == 1


def test_broker_session_rerenders_login_on_wrong_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _st = _make_broker_test_client(monkeypatch)

    resp = client.post(
        "/share/session",
        data={"email": "nobody@example.com", "password": "wrong", "mode": "signin", "next": "/"},
        follow_redirects=False,
    )

    assert resp.status_code == 401
    assert "Incorrect email or password" in resp.text


# ---------------------------------------------------------------------------
# Broker browser OAuth (Continue with Google)
# ---------------------------------------------------------------------------


def _start_broker_oauth(client: TestClient, next_path: str) -> str:
    """Drive /share/oauth/google/start and return the signed state from the provider redirect."""
    resp = client.get(f"/share/oauth/google/start?next={quote(next_path, safe='')}", follow_redirects=False)
    assert resp.status_code == 302
    return parse_qs(urlsplit(resp.headers["location"]).query)["state"][0]


def test_broker_login_page_offers_google_only_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)

    without_provider = client.get("/share/login?next=/")
    assert "Continue with Google" not in without_provider.text

    st_backend.register_provider("google")
    with_provider = client.get("/share/login?next=/share/authorize%3Fa%3Db")
    assert "Continue with Google" in with_provider.text
    assert "/share/oauth/google/start?next=%2Fshare%2Fauthorize%3Fa%3Db" in with_provider.text


def test_broker_oauth_start_redirects_with_signed_state_and_nonce_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    st_backend.register_provider("google")

    resp = client.get("/share/oauth/google/start?next=%2Fshare%2Fauthorize%3Fa%3Db", follow_redirects=False)

    assert resp.status_code == 302
    location = urlsplit(resp.headers["location"])
    assert location.netloc == "google.example.com"
    query = parse_qs(location.query)
    # The redirect URI is this broker's own web callback, derived from the request.
    assert query["redirect_uri"] == ["https://testserver/share/oauth/google/callback"]
    # The state is self-contained and signed: nonce + where to resume.
    claims = pyjwt.decode(query["state"][0], _TEST_BROKER_KEY.public_key(), algorithms=["RS256"])
    assert claims["purpose"] == "broker_oauth"
    assert claims["next"] == "/share/authorize?a=b"
    set_cookie = resp.headers["set-cookie"]
    assert "imbue_oauth_nonce=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert claims["nonce"] == client.cookies.get("imbue_oauth_nonce")


def test_broker_oauth_start_honors_accounts_base_url_for_the_redirect_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    st_backend.register_provider("google")
    monkeypatch.setenv("ACCOUNTS_BASE_URL", "https://accounts.example.com/")

    resp = client.get("/share/oauth/google/start?next=%2F", follow_redirects=False)

    query = parse_qs(urlsplit(resp.headers["location"]).query)
    assert query["redirect_uri"] == ["https://accounts.example.com/share/oauth/google/callback"]


def test_broker_oauth_start_404s_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _st = _make_broker_test_client(monkeypatch)

    resp = client.get("/share/oauth/google/start?next=%2F", follow_redirects=False)

    assert resp.status_code == 404


def test_broker_oauth_callback_signs_in_and_resumes_the_share_authorize(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full browser flow: start -> provider callback -> SSO cookie -> /share/authorize hands off a token."""
    client, backend, st_backend = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    st_backend.register_provider("google", email="visitor@example.com", is_verified=True)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"
    next_path = (
        f"/share/authorize?{urlencode({'machine_domain': domain, 'next': '', 'callback_origin': callback_origin, 'state': 'n-1'})}"
    )

    state = _start_broker_oauth(client, next_path)
    resp = client.get(f"/share/oauth/google/callback?code=code-1&state={state}", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == next_path
    assert "imbue_sso_session=" in resp.headers["set-cookie"]

    # The SSO cookie now carries the OAuth session; /share/authorize resolves
    # it to the OAuth account and mints the handoff token for that visitor.
    authorize = client.get(next_path, follow_redirects=False)

    assert authorize.status_code == 302
    assert authorize.headers["location"].startswith(f"{callback_origin}/_auth/callback?")
    token = parse_qs(urlsplit(authorize.headers["location"]).query)["token"][0]
    claims = pyjwt.decode(token, _TEST_BROKER_KEY.public_key(), algorithms=["RS256"], audience=domain)
    assert claims["email"] == "visitor@example.com"


def test_broker_oauth_callback_rejects_a_missing_nonce_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    st_backend.register_provider("google")
    state = _start_broker_oauth(client, "/share/authorize?a=b")
    client.cookies.delete("imbue_oauth_nonce", path="/share/oauth")

    resp = client.get(f"/share/oauth/google/callback?code=code-1&state={state}", follow_redirects=False)

    assert resp.status_code == 401
    assert "could not be verified" in resp.text


def test_broker_oauth_callback_rejects_garbage_or_wrong_purpose_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    st_backend.register_provider("google")

    garbage = client.get("/share/oauth/google/callback?code=c&state=not-a-jwt", follow_redirects=False)
    assert garbage.status_code == 401
    assert "invalid or has expired" in garbage.text

    # A share handoff token is a valid signature under the same key but the
    # wrong purpose; it must not open the OAuth callback.
    handoff = mint_share_handoff_token(
        signing_key=_TEST_BROKER_KEY,
        user_id=_STUB_USER_ID,
        email=_STUB_EMAIL,
        machine_domain="x.example.com",
        nonce="n",
    )
    wrong_purpose = client.get(f"/share/oauth/google/callback?code=c&state={handoff}", follow_redirects=False)
    assert wrong_purpose.status_code == 401


def test_broker_oauth_callback_reports_provider_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    st_backend.register_provider("google")
    state = _start_broker_oauth(client, "/share/authorize?a=b")

    resp = client.get(f"/share/oauth/google/callback?error=access_denied&state={state}", follow_redirects=False)

    assert resp.status_code == 401
    assert "cancelled" in resp.text


def test_broker_oauth_callback_refuses_an_email_registered_with_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    signup = st_backend.sign_up(tenant_id="public", email="alice@example.com", password="pw-123456")
    assert isinstance(signup, EPSignUpOkResult)
    st_backend.register_provider("google", email="alice@example.com")
    state = _start_broker_oauth(client, "/share/authorize?a=b")

    resp = client.get(f"/share/oauth/google/callback?code=code-1&state={state}", follow_redirects=False)

    assert resp.status_code == 401
    assert "already signs in with a password" in resp.text
    assert "imbue_sso_session=" not in resp.headers.get("set-cookie", "")


def test_broker_oauth_callback_shows_verify_page_for_an_unverified_provider_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    st_backend.register_provider("google", email="fresh@example.com", is_verified=False)
    state = _start_broker_oauth(client, "/share/authorize?a=b")

    resp = client.get(f"/share/oauth/google/callback?code=code-1&state={state}", follow_redirects=False)

    assert resp.status_code == 200
    assert "Check your inbox" in resp.text
    assert len(st_backend.sent_verification_emails) == 1
    # The session cookie is still set, matching the password flow: reloading
    # the share link after verifying continues without another sign-in.
    assert "imbue_sso_session=" in resp.headers["set-cookie"]
