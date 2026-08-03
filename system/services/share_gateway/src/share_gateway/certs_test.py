from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtensionOID
from cryptography.x509.oid import NameOID

from share_gateway.certs import build_share_csr
from share_gateway.certs import cert_matches_share
from share_gateway.certs import cert_needs_renewal
from share_gateway.certs import load_or_create_workspace_key

_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.imbueminds.com"


def _self_signed(sans: list[str], days_valid: int) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=days_valid))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in sans]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")


def test_workspace_key_is_created_once_with_0600(tmp_path: Path) -> None:
    key_path = tmp_path / "key.pem"
    first = load_or_create_workspace_key(key_path)
    second = load_or_create_workspace_key(key_path)
    assert first.private_numbers() == second.private_numbers()
    assert (key_path.stat().st_mode & 0o777) == 0o600


def test_build_share_csr_claims_domain_and_wildcard_only(tmp_path: Path) -> None:
    key = load_or_create_workspace_key(tmp_path / "key.pem")

    csr_pem = build_share_csr(key, _DOMAIN)

    csr = x509.load_pem_x509_csr(csr_pem.encode())
    assert csr.is_signature_valid
    sans = csr.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    assert set(sans.get_values_for_type(x509.DNSName)) == {_DOMAIN, f"*.{_DOMAIN}"}
    assert list(csr.subject) == []


def test_cert_matches_share_compares_sans() -> None:
    good = _self_signed([_DOMAIN, f"*.{_DOMAIN}"], days_valid=60)
    assert cert_matches_share(good, _DOMAIN) is True
    wrong = _self_signed(["other.example.com"], days_valid=60)
    assert cert_matches_share(wrong, _DOMAIN) is False
    assert cert_matches_share("garbage", _DOMAIN) is False


def test_cert_needs_renewal_by_threshold() -> None:
    fresh = _self_signed([_DOMAIN], days_valid=60)
    assert cert_needs_renewal(fresh) is False
    expiring = _self_signed([_DOMAIN], days_valid=10)
    assert cert_needs_renewal(expiring) is True
    assert cert_needs_renewal("garbage") is True
