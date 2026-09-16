"""Workspace TLS materials: key + CSR generation and the connector cert exchange.

The private key is generated here and never leaves the container. The CSR
claims exactly the workspace domain + its wildcard; the connector (which holds
the DNS credential) completes ACME DNS-01 and returns the signed chain. Key
and cert persist under ``data/.secrets/share_tls/`` so a re-share skips
reissuance while the cert is still fresh.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtensionOID

_WORKSPACE_KEY_BITS = 2048
_CERT_REQUEST_TIMEOUT_SECONDS = 300.0
RENEWAL_THRESHOLD_DAYS = 30


# Connector refusals a later attempt may clear: the connector is throttling
# this share (429), timed out reading the request (408), or failed on its own
# side (5xx). Every other 4xx means the connector rejected the request itself
# (an invalid CSR, an unknown relay token), which no retry can fix.
_RETRYABLE_REFUSAL_STATUS_CODES = frozenset({408, 429})


class CertProvisioningError(RuntimeError):
    """Raised when the connector could not sign the workspace's CSR.

    ``is_retryable`` separates failures a later attempt may clear (the
    connector unreachable, a 5xx, a 429) from ones it never will (the
    connector refused the request itself); ``retry_after_seconds`` carries the
    connector's ``Retry-After`` when it sent one.
    """

    def __init__(self, message: str, is_retryable: bool, retry_after_seconds: int | None) -> None:
        super().__init__(message)
        self.is_retryable = is_retryable
        self.retry_after_seconds = retry_after_seconds


def classify_connector_refusal(status_code: int, retry_after_header: str | None) -> tuple[bool, int | None]:
    """(is_retryable, retry_after_seconds) for a non-200 connector answer.

    Only the delay-seconds form of ``Retry-After`` is honored; the HTTP-date
    form is ignored (the connector never sends it).
    """
    is_retryable = status_code >= 500 or status_code in _RETRYABLE_REFUSAL_STATUS_CODES
    retry_after_seconds = None
    if retry_after_header is not None and retry_after_header.strip().isdigit():
        retry_after_seconds = int(retry_after_header.strip())
    return is_retryable, retry_after_seconds


def load_or_create_workspace_key(key_path: Path) -> rsa.RSAPrivateKey:
    """The workspace's TLS private key, generated once and persisted with 0600."""
    if key_path.exists():
        loaded = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        assert isinstance(loaded, rsa.RSAPrivateKey)
        return loaded
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=_WORKSPACE_KEY_BITS)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return private_key


def build_share_csr(private_key: rsa.RSAPrivateKey, workspace_domain: str) -> str:
    """A PEM CSR claiming exactly the workspace domain + its wildcard (SAN-only; names exceed the CN limit)."""
    builder = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([]))
    builder = builder.add_extension(
        x509.SubjectAlternativeName([x509.DNSName(workspace_domain), x509.DNSName(f"*.{workspace_domain}")]),
        critical=False,
    )
    csr = builder.sign(private_key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.PEM).decode("utf-8")


def request_certificate(connector_url: str, relay_token: str, csr_pem: str) -> str:
    """POST the CSR to the connector's ACME endpoint and return the PEM chain."""
    try:
        response = httpx.post(
            f"{connector_url}/shares/cert",
            json={"csr_pem": csr_pem},
            headers={"Authorization": f"Bearer {relay_token}"},
            timeout=_CERT_REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise CertProvisioningError(
            f"cert request to the connector failed: {exc}", is_retryable=True, retry_after_seconds=None
        ) from exc
    if response.status_code != 200:
        is_retryable, retry_after_seconds = classify_connector_refusal(
            response.status_code, response.headers.get("Retry-After")
        )
        raise CertProvisioningError(
            f"connector refused the CSR ({response.status_code}): {response.text[:500]}",
            is_retryable=is_retryable,
            retry_after_seconds=retry_after_seconds,
        )
    try:
        cert_chain_pem = response.json().get("cert_chain_pem")
    except ValueError as exc:
        raise CertProvisioningError(
            "connector returned a non-JSON certificate response", is_retryable=True, retry_after_seconds=None
        ) from exc
    if not isinstance(cert_chain_pem, str) or not cert_chain_pem:
        raise CertProvisioningError(
            "connector returned no certificate chain", is_retryable=True, retry_after_seconds=None
        )
    return cert_chain_pem


def cert_matches_share(cert_pem_text: str, workspace_domain: str) -> bool:
    """Whether an on-disk cert covers this share's names (a re-share after a region move must reissue)."""
    try:
        leaf = x509.load_pem_x509_certificate(cert_pem_text.encode("utf-8"))
        san_extension = leaf.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    except (ValueError, x509.ExtensionNotFound):
        return False
    sans = set(san_extension.value.get_values_for_type(x509.DNSName))
    return sans == {workspace_domain, f"*.{workspace_domain}"}


def cert_needs_renewal(cert_pem_text: str, threshold_days: int = RENEWAL_THRESHOLD_DAYS) -> bool:
    """Whether the leaf expires within the renewal threshold (or cannot be parsed at all)."""
    try:
        leaf = x509.load_pem_x509_certificate(cert_pem_text.encode("utf-8"))
    except ValueError:
        return True
    return leaf.not_valid_after_utc <= datetime.now(timezone.utc) + timedelta(days=threshold_days)


def ensure_share_certificate(
    key_path: Path,
    cert_path: Path,
    workspace_domain: str,
    connector_url: str,
    relay_token: str,
) -> None:
    """Make sure a fresh cert for this share's names is on disk, issuing/renewing via the connector when not."""
    load_or_create_workspace_key(key_path)
    if cert_path.exists():
        existing = cert_path.read_text()
        if cert_matches_share(existing, workspace_domain) and not cert_needs_renewal(existing):
            return
    private_key = load_or_create_workspace_key(key_path)
    csr_pem = build_share_csr(private_key, workspace_domain)
    cert_chain_pem = request_certificate(connector_url, relay_token, csr_pem)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_text(cert_chain_pem)
