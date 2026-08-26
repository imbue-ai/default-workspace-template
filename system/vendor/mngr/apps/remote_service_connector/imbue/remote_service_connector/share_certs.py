"""ACME DNS-01 certificate issuance for shared workspaces (/shares/cert).

The workspace generates its TLS key and sends a CSR (the private key never
leaves the container); the connector -- sole custodian of the Cloudflare DNS
credential -- completes the DNS-01 challenges and returns the signed chain.
CAs are tried in the configured order (ACME_CA_LIST), each optionally with
External Account Binding (ZeroSSL / Google Trust Services), so issuance
survives one CA's rate limits or outage.
"""

import functools
import json
import logging
import os
import re
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Final
from typing import Protocol

import httpx
import josepy
from acme import challenges as acme_challenges
from acme import client as acme_client_module
from acme import errors as acme_errors
from acme import messages as acme_messages
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_delay
from tenacity import wait_fixed

import imbue.remote_service_connector.shares as shares_module
from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector import db
from imbue.remote_service_connector.cloudflare import CF_BASE_URL
from imbue.remote_service_connector.cloudflare import cf_check
from imbue.remote_service_connector.errors import AcmeIssuanceError
from imbue.remote_service_connector.errors import CloudflareApiError
from imbue.remote_service_connector.errors import ConnectorError
from imbue.remote_service_connector.errors import InvalidCsrError
from imbue.remote_service_connector.errors import MissingShareConfigError
from imbue.remote_service_connector.http_api import handle_endpoint_errors
from imbue.remote_service_connector.shares import require_share_env

logger = logging.getLogger(__name__)

router = APIRouter()

_ACME_USER_AGENT = "imbue-remote-service-connector"
_ACME_ACCOUNT_KEY_BITS = 2048
_ACME_MIN_CSR_RSA_BITS = 2048
_ACME_FINALIZE_DEADLINE_SECONDS = 120
_ACME_TXT_PROPAGATION_TIMEOUT_SECONDS = 90
_ACME_TXT_POLL_INTERVAL_SECONDS = 3
_ACME_TXT_RECORD_TTL_SECONDS = 60
_DNS_OVER_HTTPS_URL = "https://cloudflare-dns.com/dns-query"


class AcmeCaConfig(BaseModel):
    """One entry of the ordered ACME CA list, with optional EAB credentials."""

    name: str
    directory_url: str
    eab_kid: str | None = None
    eab_hmac_key: str | None = None


def parse_acme_ca_list(raw: str) -> list[tuple[str, str]]:
    """Parse ``ACME_CA_LIST`` (``letsencrypt=https://...,zerossl=https://...``) into ordered (name, url) pairs."""
    pairs: list[tuple[str, str]] = []
    for entry in raw.split(","):
        stripped_entry = entry.strip()
        if not stripped_entry:
            continue
        name, separator, directory_url = stripped_entry.partition("=")
        if not separator or not name.strip() or not directory_url.strip():
            raise MissingShareConfigError(f"ACME_CA_LIST (malformed entry {stripped_entry!r})")
        pairs.append((name.strip(), directory_url.strip()))
    if not pairs:
        raise MissingShareConfigError("ACME_CA_LIST")
    return pairs


def acme_ca_configs_from_env() -> list[AcmeCaConfig]:
    """The ordered CA list from env, each with its EAB credentials (``ACME_EAB_KID_<NAME>`` / ``ACME_EAB_HMAC_<NAME>``)."""
    configs: list[AcmeCaConfig] = []
    for name, directory_url in parse_acme_ca_list(require_share_env("ACME_CA_LIST")):
        env_suffix = re.sub(r"[^A-Z0-9]", "_", name.upper())
        eab_kid = os.environ.get(f"ACME_EAB_KID_{env_suffix}", "")
        eab_hmac_key = os.environ.get(f"ACME_EAB_HMAC_{env_suffix}", "")
        configs.append(
            AcmeCaConfig(
                name=name,
                directory_url=directory_url,
                eab_kid=eab_kid or None,
                eab_hmac_key=eab_hmac_key or None,
            )
        )
    return configs


def validate_share_csr(csr_pem: str, workspace_domain: str) -> None:
    """Validate a workspace's CSR: parseable, self-signature valid, sane key, and exactly the share's SANs.

    The SAN set must be exactly ``{workspace_domain, *.workspace_domain}`` --
    anything else would let a workspace request a certificate for names its
    relay token does not own.
    """
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode("utf-8"))
    except ValueError as exc:
        raise InvalidCsrError(f"CSR is not valid PEM: {exc}") from exc
    if not csr.is_signature_valid:
        raise InvalidCsrError("CSR signature is invalid")
    public_key = csr.public_key()
    if isinstance(public_key, rsa.RSAPublicKey):
        if public_key.key_size < _ACME_MIN_CSR_RSA_BITS:
            raise InvalidCsrError(f"CSR RSA key must be >= {_ACME_MIN_CSR_RSA_BITS} bits, got {public_key.key_size}")
    elif isinstance(public_key, ec.EllipticCurvePublicKey):
        pass
    else:
        raise InvalidCsrError("CSR public key must be RSA or ECDSA")
    try:
        san_extension = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound as exc:
        raise InvalidCsrError("CSR has no subjectAltName extension") from exc
    claimed_names = set(san_extension.value.get_values_for_type(x509.DNSName))
    expected_names = {workspace_domain, f"*.{workspace_domain}"}
    if claimed_names != expected_names:
        raise InvalidCsrError(f"CSR SANs must be exactly {sorted(expected_names)}, got {sorted(claimed_names)}")


def extract_cert_chain_metadata(cert_chain_pem: str) -> tuple[str, list[str]]:
    """Return (leaf not_after ISO timestamp, leaf SANs) from a PEM chain (leaf first)."""
    try:
        leaf = x509.load_pem_x509_certificate(cert_chain_pem.encode("utf-8"))
    except ValueError as exc:
        raise AcmeIssuanceError(f"CA returned an unparseable certificate chain: {exc}") from exc
    san_extension = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    sans = list(san_extension.value.get_values_for_type(x509.DNSName))
    return leaf.not_valid_after_utc.isoformat(), sans


class Dns01Ops(Protocol):
    """The two DNS operations DNS-01 issuance needs, so tests can fake them."""

    def create_txt_record(self, record_name: str, content: str) -> str: ...
    def delete_txt_record(self, record_id: str) -> None: ...


class CloudflareDns01Ops(BaseModel):
    """Dns01Ops against the content domain's Cloudflare zone (same token/zone as the rest of the connector)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    zone_id: str
    client: httpx.Client

    def create_txt_record(self, record_name: str, content: str) -> str:
        response = self.client.post(
            f"/zones/{self.zone_id}/dns_records",
            json={"type": "TXT", "name": record_name, "content": content, "ttl": _ACME_TXT_RECORD_TTL_SECONDS},
        )
        return str(cf_check(response)["result"]["id"])

    def delete_txt_record(self, record_id: str) -> None:
        response = self.client.delete(f"/zones/{self.zone_id}/dns_records/{record_id}")
        cf_check(response)


@functools.cache
def get_dns01_ops() -> Dns01Ops:
    # require_share_env (rather than bare indexing) so a tier missing the
    # cloudflare secret surfaces as the 503 sharing-not-configured diagnostic
    # like every other sharing config value, not an opaque KeyError 500.
    client = httpx.Client(
        base_url=CF_BASE_URL,
        headers={"Authorization": f"Bearer {require_share_env('CLOUDFLARE_API_TOKEN')}"},
        timeout=30.0,
    )
    return CloudflareDns01Ops(zone_id=require_share_env("CLOUDFLARE_ZONE_ID"), client=client)


class AcmeAccountStore(Protocol):
    """Abstraction over the acme_accounts table so issuance is unit-testable."""

    def get_account(self, ca_name: str, directory_url: str) -> dict[str, Any] | None: ...
    def save_account(
        self, ca_name: str, directory_url: str, account_key_pem: str, account_uri: str, eab_kid: str | None
    ) -> None: ...


class PostgresAcmeAccountStore:
    """AcmeAccountStore backed by the connector's existing Neon DB."""

    def get_account(self, ca_name: str, directory_url: str) -> dict[str, Any] | None:
        with db.pooled_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT account_key_pem, account_uri FROM acme_accounts WHERE ca_name = %s AND directory_url = %s",
                    (ca_name, directory_url),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return {"account_key_pem": row[0], "account_uri": row[1]}

    def save_account(
        self, ca_name: str, directory_url: str, account_key_pem: str, account_uri: str, eab_kid: str | None
    ) -> None:
        with db.pooled_db_connection() as conn:
            with conn:
                with conn.cursor() as cur:
                    # ON CONFLICT DO NOTHING: two concurrent first-issuance
                    # calls for the same CA both create an ACME account and race
                    # to INSERT; the loser must no-op rather than raise an
                    # IntegrityError that would surface as an unhandled 500.
                    cur.execute(
                        "INSERT INTO acme_accounts (ca_name, directory_url, account_key_pem, account_uri, eab_kid) "
                        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (ca_name, directory_url) DO NOTHING",
                        (ca_name, directory_url, account_key_pem, account_uri, eab_kid),
                    )


@functools.cache
def get_acme_account_store() -> AcmeAccountStore:
    return PostgresAcmeAccountStore()


class _TxtNotPropagatedError(ConnectorError, RuntimeError):
    """Internal: the expected TXT values are not yet visible; tenacity retries the probe."""


def _query_txt_values_via_doh(record_name: str) -> set[str]:
    """Resolve a TXT record via DNS-over-HTTPS (authoritative through Cloudflare's resolver)."""
    response = httpx.get(
        _DNS_OVER_HTTPS_URL,
        params={"name": record_name, "type": "TXT"},
        headers={"accept": "application/dns-json"},
        timeout=10.0,
    )
    response.raise_for_status()
    answers = response.json().get("Answer") or []
    return {str(answer.get("data", "")).strip('"') for answer in answers}


@retry(
    retry=retry_if_exception_type((_TxtNotPropagatedError, httpx.HTTPError)),
    stop=stop_after_delay(_ACME_TXT_PROPAGATION_TIMEOUT_SECONDS),
    wait=wait_fixed(_ACME_TXT_POLL_INTERVAL_SECONDS),
    reraise=True,
)
def _wait_for_txt_propagation(expected_values_by_record_name: dict[str, set[str]]) -> None:
    """Poll public DNS until every challenge TXT record is visible.

    ACME servers validate a DNS-01 challenge only once per answer, so
    answering before the record resolves would fail the whole order.
    """
    for record_name, expected_values in expected_values_by_record_name.items():
        visible_values = _query_txt_values_via_doh(record_name)
        if not expected_values <= visible_values:
            raise _TxtNotPropagatedError(f"TXT {record_name} not fully propagated yet")


def _load_acme_account_key(account_key_pem: str) -> josepy.JWKRSA:
    private_key = serialization.load_pem_private_key(account_key_pem.encode("utf-8"), password=None)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise AcmeIssuanceError("stored ACME account key is not an RSA private key")
    return josepy.JWKRSA(key=private_key)


def _generate_acme_account_key() -> tuple[josepy.JWKRSA, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=_ACME_ACCOUNT_KEY_BITS)
    account_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return josepy.JWKRSA(key=private_key), account_key_pem


def _acme_client_for_ca(ca: AcmeCaConfig, account_store: AcmeAccountStore) -> acme_client_module.ClientV2:
    """Build an ACME client with a registered account for one CA, creating and persisting the account on first use."""
    existing = account_store.get_account(ca.name, ca.directory_url)
    if existing is not None:
        account_key = _load_acme_account_key(str(existing["account_key_pem"]))
        registration = acme_messages.RegistrationResource(
            body=acme_messages.Registration(),
            uri=str(existing["account_uri"]),
        )
        network = acme_client_module.ClientNetwork(account_key, account=registration, user_agent=_ACME_USER_AGENT)
        directory = acme_client_module.ClientV2.get_directory(ca.directory_url, network)
        return acme_client_module.ClientV2(directory, net=network)

    account_key, account_key_pem = _generate_acme_account_key()
    network = acme_client_module.ClientNetwork(account_key, user_agent=_ACME_USER_AGENT)
    directory = acme_client_module.ClientV2.get_directory(ca.directory_url, network)
    client = acme_client_module.ClientV2(directory, net=network)
    external_account_binding = None
    if ca.eab_kid and ca.eab_hmac_key:
        external_account_binding = acme_messages.ExternalAccountBinding.from_data(
            account_public_key=account_key.public_key(),
            kid=ca.eab_kid,
            hmac_key=ca.eab_hmac_key,
            directory=directory,
        )
    registration_resource = client.new_account(
        acme_messages.NewRegistration.from_data(
            terms_of_service_agreed=True,
            external_account_binding=external_account_binding,
        )
    )
    account_store.save_account(ca.name, ca.directory_url, account_key_pem, str(registration_resource.uri), ca.eab_kid)
    return client


def _issue_certificate_with_ca(
    ca: AcmeCaConfig,
    csr_pem: str,
    dns_ops: Dns01Ops,
    account_store: AcmeAccountStore,
) -> str:
    """Run one CA's full DNS-01 order for the CSR and return the PEM chain."""
    client = _acme_client_for_ca(ca, account_store)
    order = client.new_order(csr_pem.encode("utf-8"))
    created_record_ids: list[str] = []
    try:
        # Publish every authorization's TXT record first, then wait for all of
        # them at once (one propagation wait instead of one per name).
        challenge_bodies: list[Any] = []
        expected_values_by_record_name: dict[str, set[str]] = {}
        for authorization in order.authorizations:
            dns_challenge_body = None
            for challenge_body in authorization.body.challenges:
                if isinstance(challenge_body.chall, acme_challenges.DNS01):
                    dns_challenge_body = challenge_body
                    break
            if dns_challenge_body is None:
                raise AcmeIssuanceError(
                    f"{ca.name} offered no dns-01 challenge for {authorization.body.identifier.value}"
                )
            validation_value = dns_challenge_body.chall.validation(client.net.key)
            record_name = dns_challenge_body.chall.validation_domain_name(authorization.body.identifier.value)
            record_id = dns_ops.create_txt_record(record_name, validation_value)
            created_record_ids.append(record_id)
            expected_values_by_record_name.setdefault(record_name, set()).add(validation_value)
            challenge_bodies.append(dns_challenge_body)
        _wait_for_txt_propagation(expected_values_by_record_name)
        for challenge_body in challenge_bodies:
            client.answer_challenge(challenge_body, challenge_body.chall.response(client.net.key))
        deadline = datetime.now() + timedelta(seconds=_ACME_FINALIZE_DEADLINE_SECONDS)
        finalized_order = client.poll_and_finalize(order, deadline=deadline)
    finally:
        for record_id in created_record_ids:
            try:
                dns_ops.delete_txt_record(record_id)
            except (httpx.HTTPError, CloudflareApiError) as exc:
                emit_metric("cloudflare_api_failed", 1, {"operation": "acme_txt_record_cleanup"})
                logger.warning("Failed to clean up ACME challenge TXT record %s", record_id, exc_info=exc)
    fullchain_pem = finalized_order.fullchain_pem
    if not fullchain_pem:
        raise AcmeIssuanceError(f"{ca.name} finalized the order but returned no certificate chain")
    return str(fullchain_pem)


def issue_share_certificate(
    csr_pem: str,
    dns_ops: Dns01Ops,
    account_store: AcmeAccountStore,
    ca_configs: list[AcmeCaConfig],
) -> tuple[str, str]:
    """Issue a certificate for a validated share CSR, trying each configured CA in order.

    Returns ``(cert_chain_pem, ca_name)`` from the first CA that succeeds.
    """
    last_error: Exception | None = None
    for ca in ca_configs:
        try:
            return _issue_certificate_with_ca(ca, csr_pem, dns_ops, account_store), ca.name
        except (
            AcmeIssuanceError,
            acme_errors.Error,
            httpx.HTTPError,
            _TxtNotPropagatedError,
            CloudflareApiError,
            OSError,
        ) as exc:
            # Expected now and then (CA outages, rate limits); the next CA
            # is tried, and the metric's rate shows a CA degrading.
            emit_metric("acme_ca_issuance_failed", 1, {"ca": ca.name})
            logger.warning("ACME issuance via %s failed", ca.name, exc_info=exc)
            last_error = exc
    raise AcmeIssuanceError("every configured ACME CA failed to issue the certificate") from last_error


class IssueShareCertRequest(BaseModel):
    csr_pem: str = Field(description="PEM CSR for exactly the share's workspace domain + wildcard")


# ACME issuance ceiling per share per rolling day. A relay-token holder could
# otherwise loop issuance, churning real DNS TXT records and burning CA order
# quota (Let's Encrypt's duplicate-certificate limit is 5/week). Legitimate
# traffic is one first issuance plus a daily renewal check that only reissues
# under 30 days to expiry, so five a day is generous.
_MAX_CERT_ISSUANCES_PER_DAY: Final[int] = 5


@router.post("/shares/cert")
def issue_share_cert(request: Request, body: IssueShareCertRequest) -> dict[str, object]:
    """Sign a shared workspace's CSR via ACME DNS-01 and return the chain.

    Authenticated by the share's relay token (Bearer), so only the workspace
    that holds the token can obtain certificates -- and only for exactly its
    own workspace domain + wildcard (enforced against the CSR's SANs). Used
    for both first issuance and renewal; the workspace keeps its private key.
    """
    with handle_endpoint_errors():
        share = shares_module.require_active_share_by_relay_token(request)
        store = shares_module.get_share_store()
        workspace_domain = str(share["workspace_domain"])
        issued_today = store.count_certs_issued_in_last_day(str(share["host_id"]), str(share["user_id"]))
        if issued_today >= _MAX_CERT_ISSUANCES_PER_DAY:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"This share has been issued {issued_today} certificates in the last 24 hours "
                    f"(limit {_MAX_CERT_ISSUANCES_PER_DAY}); wait before requesting another."
                ),
            )
        validate_share_csr(body.csr_pem, workspace_domain)
        cert_chain_pem, ca_name = issue_share_certificate(
            csr_pem=body.csr_pem,
            dns_ops=get_dns01_ops(),
            account_store=get_acme_account_store(),
            ca_configs=acme_ca_configs_from_env(),
        )
        not_after, sans = extract_cert_chain_metadata(cert_chain_pem)
        store.record_issued_cert(
            workspace_domain=workspace_domain,
            host_id=str(share["host_id"]),
            user_label=str(share["user_id"]),
            ca_name=ca_name,
            cert_chain_pem=cert_chain_pem,
            sans_json=json.dumps(sans),
            not_after=not_after,
        )
        return {
            "cert_chain_pem": cert_chain_pem,
            "ca_name": ca_name,
            "not_after": not_after,
            "sans": sans,
        }
