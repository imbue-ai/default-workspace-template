"""Tests for the shared-workspace ACME DNS-01 issuance (/shares/cert)."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import imbue.remote_service_connector.share_certs as share_certs_module
from imbue.remote_service_connector.errors import InvalidCsrError
from imbue.remote_service_connector.errors import MissingShareConfigError
from imbue.remote_service_connector.share_certs import AcmeCaConfig
from imbue.remote_service_connector.share_certs import acme_ca_configs_from_env
from imbue.remote_service_connector.share_certs import extract_cert_chain_metadata
from imbue.remote_service_connector.share_certs import parse_acme_ca_list
from imbue.remote_service_connector.share_certs import validate_share_csr
from imbue.remote_service_connector.testing import _CONTENT_DOMAIN
from imbue.remote_service_connector.testing import _OTHER_HOST_ID
from imbue.remote_service_connector.testing import _SHARE_STUB_HOST_ID
from imbue.remote_service_connector.testing import _SHARE_STUB_USER_LABEL
from imbue.remote_service_connector.testing import _make_share_test_client
from imbue.remote_service_connector.testing import _share_headers

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
    domain = f"{_SHARE_STUB_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.{_CONTENT_DOMAIN}"
    validate_share_csr(_make_share_csr(domain), domain)


def test_validate_share_csr_rejects_wrong_or_extra_sans() -> None:
    domain = f"{_SHARE_STUB_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.{_CONTENT_DOMAIN}"
    with pytest.raises(InvalidCsrError):
        validate_share_csr(_make_share_csr(domain, sans=[domain]), domain)
    with pytest.raises(InvalidCsrError):
        validate_share_csr(_make_share_csr(domain, sans=[domain, f"*.{domain}", "evil.example.com"]), domain)
    other_domain = domain.replace(_SHARE_STUB_HOST_ID, _OTHER_HOST_ID)
    with pytest.raises(InvalidCsrError):
        validate_share_csr(_make_share_csr(other_domain), domain)


def test_validate_share_csr_rejects_garbage_and_weak_keys() -> None:
    domain = f"{_SHARE_STUB_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.{_CONTENT_DOMAIN}"
    with pytest.raises(InvalidCsrError):
        validate_share_csr("not a csr", domain)
    weak_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    with pytest.raises(InvalidCsrError):
        validate_share_csr(_make_share_csr(domain, key=weak_key), domain)


def test_extract_cert_chain_metadata_reads_leaf() -> None:
    domain = f"{_SHARE_STUB_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.{_CONTENT_DOMAIN}"
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
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    client.delete(f"/shares/{_SHARE_STUB_HOST_ID}", headers=_share_headers())

    resp = client.post(
        "/shares/cert", json={"csr_pem": "x"}, headers={"Authorization": f"Bearer {created['relay_token']}"}
    )

    assert resp.status_code == 401


def test_issue_share_cert_rate_limits_per_share_per_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sixth issuance inside a day 429s before any ACME or DNS work happens."""
    client, backend = _make_share_test_client(monkeypatch)
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    for _ in range(5):
        backend.issued_cert_rows.append(
            {
                "workspace_domain": created["workspace_domain"],
                "host_id": _SHARE_STUB_HOST_ID,
                "user_id": _SHARE_STUB_USER_LABEL,
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
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
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
    created = client.post("/shares", json={"host_id": _SHARE_STUB_HOST_ID}, headers=_share_headers()).json()
    domain = created["workspace_domain"]
    chain = _make_self_signed_chain([domain, f"*.{domain}"])

    def _stub_issue(
        csr_pem: str, dns_ops: object, account_store: object, ca_configs: list[AcmeCaConfig]
    ) -> tuple[str, str]:
        assert ca_configs and ca_configs[0].name == "letsencrypt"
        return chain, "letsencrypt"

    monkeypatch.setattr(share_certs_module, "issue_share_certificate", _stub_issue)

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

    status = client.get(f"/shares/{_SHARE_STUB_HOST_ID}/status", headers=_share_headers()).json()
    assert status["cert_not_after"] == body["not_after"]
