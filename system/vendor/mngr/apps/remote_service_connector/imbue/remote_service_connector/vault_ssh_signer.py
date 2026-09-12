"""Signing the connector's management SSH certificates with the tier's Vault SSH CA over HTTP.

Only the ``ssh_cert_refresh`` cron uses this: it logs in with the tier's AppRole
(the one Vault credential the connector holds, scoped to signing the connector
and analytics roles and stamped into the ``ssh-ca`` Modal Secret by ``minds-admin
env deploy``), asks the ``minds-<tier>-ssh`` mount to sign a freshly generated public
key per role, and hands the resulting bundles to ``ssh_certs`` for storage.
"""

import os
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Final

import httpx
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr

from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_ANALYTICS_PRINCIPALS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_CONNECTOR_PRINCIPALS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_VAULT_ROLE_ANALYTICS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_VAULT_ROLE_CONNECTOR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import ssh_ca_vault_mount
from imbue.remote_service_connector.errors import ConnectorError
from imbue.remote_service_connector.ssh_certs import SSH_CERT_DICT_KEY_ANALYTICS
from imbue.remote_service_connector.ssh_certs import SSH_CERT_DICT_KEY_CONNECTOR
from imbue.remote_service_connector.ssh_certs import SSH_CERT_TTL
from imbue.remote_service_connector.ssh_certs import SshCertificateBundle
from imbue.remote_service_connector.ssh_certs import generate_ed25519_private_key_pem_and_public_key

# The HCP Vault cluster (the same defaults the operator tooling's vault_reader
# fills in); the ``ssh-ca`` secret may override both.
DEFAULT_VAULT_ADDR: Final[str] = "https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200"
DEFAULT_VAULT_NAMESPACE: Final[str] = "admin"

# A hard bound on each Vault round trip so a wedged Vault cannot hang the cron.
VAULT_HARD_TIMEOUT_SECONDS: Final[float] = 30.0

# A stored bundle older than this has outlived two of the cron's two-hourly
# refresh ticks: the refresh is failing, not merely late.
SSH_CERT_REFRESH_OVERDUE_AFTER: Final[timedelta] = timedelta(hours=4)

# The principals each Dict entry's certificate carries, by entry key.
PRINCIPALS_BY_DICT_KEY: Final[dict[str, tuple[str, ...]]] = {
    SSH_CERT_DICT_KEY_CONNECTOR: SSH_CA_CONNECTOR_PRINCIPALS,
    SSH_CERT_DICT_KEY_ANALYTICS: SSH_CA_ANALYTICS_PRINCIPALS,
}
VAULT_ROLE_BY_DICT_KEY: Final[dict[str, str]] = {
    SSH_CERT_DICT_KEY_CONNECTOR: SSH_CA_VAULT_ROLE_CONNECTOR,
    SSH_CERT_DICT_KEY_ANALYTICS: SSH_CA_VAULT_ROLE_ANALYTICS,
}


class VaultSshSignerNotConfiguredError(ConnectorError, RuntimeError):
    """Raised when the ``ssh-ca`` secret carries no AppRole (the tier's SSH CA has not been brought up)."""


class VaultSshSignError(ConnectorError, RuntimeError):
    """Raised when Vault refuses the AppRole login or the certificate sign."""


class VaultSshSignerConfig(BaseModel):
    """Where and as whom the refresh cron signs certificates."""

    model_config = ConfigDict(frozen=True)

    vault_addr: str = Field(description="Vault API base URL")
    vault_namespace: str = Field(description="Vault namespace (HCP: ``admin``)")
    mount: str = Field(description="The tier's SSH secrets engine mount (``minds-<tier>-ssh``)")
    approle_role_id: str = Field(description="The connector AppRole's role id")
    approle_secret_id: SecretStr = Field(description="The connector AppRole's secret id")


def load_vault_ssh_signer_config(tier: str) -> VaultSshSignerConfig:
    """Read the signer config from the ``ssh-ca`` Modal Secret's env; raises when the AppRole is unset."""
    role_id = os.environ.get("VAULT_SSH_APPROLE_ROLE_ID", "")
    secret_id = os.environ.get("VAULT_SSH_APPROLE_SECRET_ID", "")
    if not role_id or not secret_id:
        raise VaultSshSignerNotConfiguredError(
            "VAULT_SSH_APPROLE_ROLE_ID / VAULT_SSH_APPROLE_SECRET_ID are not set: populate the tier's ssh-ca Vault "
            "entry (.minds/template/ssh-ca.sh) and redeploy before gen-2 boxes can be managed"
        )
    return VaultSshSignerConfig(
        vault_addr=os.environ.get("VAULT_ADDR", DEFAULT_VAULT_ADDR).rstrip("/"),
        vault_namespace=os.environ.get("VAULT_NAMESPACE", DEFAULT_VAULT_NAMESPACE),
        mount=ssh_ca_vault_mount(tier),
        approle_role_id=role_id,
        approle_secret_id=SecretStr(secret_id),
    )


def _vault_post(
    client: httpx.Client,
    config: VaultSshSignerConfig,
    path: str,
    body: dict[str, object],
    *,
    # The login token to send as X-Vault-Token; None for the login call itself.
    token: str | None,
) -> dict:
    """One Vault API POST; raises :class:`VaultSshSignError` on a non-2xx or non-JSON answer."""
    headers = {"X-Vault-Namespace": config.vault_namespace}
    if token is not None:
        headers["X-Vault-Token"] = token
    response = client.post(f"{config.vault_addr}/v1/{path}", json=body, headers=headers)
    if response.status_code >= 400:
        raise VaultSshSignError(f"Vault {path} answered {response.status_code}: {response.text[:300]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise VaultSshSignError(f"Vault {path} answered non-JSON: {response.text[:300]}") from exc
    if not isinstance(payload, dict):
        raise VaultSshSignError(f"Vault {path} answered a non-object JSON payload")
    return payload


def _approle_login(client: httpx.Client, config: VaultSshSignerConfig) -> str:
    payload = _vault_post(
        client,
        config,
        "auth/approle/login",
        {"role_id": config.approle_role_id, "secret_id": config.approle_secret_id.get_secret_value()},
        token=None,
    )
    token = (payload.get("auth") or {}).get("client_token") if isinstance(payload.get("auth"), dict) else None
    if not isinstance(token, str) or not token:
        raise VaultSshSignError("Vault AppRole login returned no client token")
    return token


def _sign(
    client: httpx.Client,
    config: VaultSshSignerConfig,
    token: str,
    role: str,
    public_key: str,
    principals: Sequence[str],
) -> str:
    payload = _vault_post(
        client,
        config,
        f"{config.mount}/sign/{role}",
        {
            "public_key": public_key,
            "valid_principals": ",".join(principals),
            "ttl": f"{int(SSH_CERT_TTL.total_seconds())}s",
            "cert_type": "user",
        },
        token=token,
    )
    signed_key = (payload.get("data") or {}).get("signed_key") if isinstance(payload.get("data"), dict) else None
    if not isinstance(signed_key, str) or not signed_key.strip():
        raise VaultSshSignError(f"Vault {config.mount}/sign/{role} returned no signed key")
    return signed_key.strip()


def sign_management_bundles(
    config: VaultSshSignerConfig, *, now: datetime | None = None, http_client: httpx.Client | None = None
) -> dict[str, SshCertificateBundle]:
    """Mint one fresh key + certificate per Dict entry (connector, analytics) through the tier's CA.

    A fresh keypair per bundle per refresh, so an old key is dead the moment its
    certificate expires. Returns the bundles keyed by their Dict entry name.
    """
    signed_at = now if now is not None else datetime.now(timezone.utc)
    owned_client = http_client is None
    client = http_client if http_client is not None else httpx.Client(timeout=VAULT_HARD_TIMEOUT_SECONDS)
    try:
        token = _approle_login(client, config)
        bundles: dict[str, SshCertificateBundle] = {}
        for dict_key, role in VAULT_ROLE_BY_DICT_KEY.items():
            private_key_pem, public_key = generate_ed25519_private_key_pem_and_public_key()
            certificate = _sign(client, config, token, role, public_key, PRINCIPALS_BY_DICT_KEY[dict_key])
            bundles[dict_key] = SshCertificateBundle(
                private_key_pem=private_key_pem,
                certificate=certificate,
                expires_at=signed_at + SSH_CERT_TTL,
                role=role,
            )
        return bundles
    finally:
        if owned_client:
            client.close()


def certificate_refresh_summary(bundles: dict[str, SshCertificateBundle]) -> dict[str, str]:
    """The log-safe summary of a refresh: which entries were written and when they expire."""
    return {dict_key: bundle.expires_at.isoformat() for dict_key, bundle in sorted(bundles.items())}


def is_refresh_overdue(expires_at: datetime, now: datetime) -> bool:
    """Whether a stored bundle is past the point the cron should have replaced it (used by the watchdog log)."""
    return expires_at - now < SSH_CERT_TTL - SSH_CERT_REFRESH_OVERDUE_AFTER
