"""The connector's management SSH credentials: a short-lived certificate for gen-2, the static pool key for gen-1.

Gen-2 boxes, slice VMs, and workspace containers trust the tier's SSH CA in
Vault (imbue-ai/mngr-internal#850), so the connector reaches them with a
certificate rather than a long-lived key. One cron (``ssh_cert_refresh`` in
``app.py``) is the only function that talks to Vault: every few hours it
generates a fresh ed25519 key, has the CA sign it for the connector and
analytics roles, and stores each bundle in a Modal Dict. Every other function
reads its bundle from the Dict (a Modal-internal read, no Vault in any request
path) and caches it in process until it nears expiry.

Gen-1 boxes and VMs still authorize the static pool key, so the credentials
are resolved per box generation until the gen-1 fleet is gone.
"""

import io
import os
import threading
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Final

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_VAULT_ROLE_ANALYTICS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_VAULT_ROLE_CONNECTOR
from imbue.remote_service_connector.errors import ConnectorError

# The Modal Dict the refresh cron writes and every SSH-bearing function reads.
# Dict names are scoped to the Modal environment, so a dev env never reads
# another env's bundles; the analytics app in the same environment reads the
# same Dict by name.
SSH_CERT_DICT_NAME: Final[str] = "ssh-management-certs"
# One entry per signing role.
SSH_CERT_DICT_KEY_CONNECTOR: Final[str] = SSH_CA_VAULT_ROLE_CONNECTOR
SSH_CERT_DICT_KEY_ANALYTICS: Final[str] = SSH_CA_VAULT_ROLE_ANALYTICS

# The certificate lifetime the cron asks Vault for, and how often it re-signs.
# The gap is the tolerated Vault outage: bundles keep working for TTL minus the
# refresh cadence after Vault stops answering.
SSH_CERT_TTL: Final[timedelta] = timedelta(hours=8)
SSH_CERT_REFRESH_CRON: Final[str] = "15 */2 * * *"
# A cached bundle is re-read from the Dict once it has less than this left, so
# a container never presents a certificate the host is about to refuse.
SSH_CERT_REREAD_MARGIN: Final[timedelta] = timedelta(hours=1)


class SshCertificateBundleMissingError(ConnectorError, RuntimeError):
    """Raised when no usable gen-2 management certificate is available (the refresh has not run, or it expired)."""


class SshCertificateBundle(BaseModel):
    """One signed management identity: the private key, its certificate, and when the certificate expires."""

    model_config = ConfigDict(frozen=True)

    private_key_pem: SecretStr = Field(
        description="OpenSSH ed25519 private key PEM (never persisted outside the Dict)"
    )
    certificate: str = Field(description="The OpenSSH certificate line (``ssh-ed25519-cert-v01@openssh.com ...``)")
    expires_at: datetime = Field(description="When the certificate stops being accepted (UTC)")
    role: str = Field(description="The Vault signing role that minted it")

    def is_fresh_at(self, now: datetime) -> bool:
        return self.expires_at - now >= SSH_CERT_REREAD_MARGIN

    def to_dict_entry(self) -> dict[str, str]:
        """The JSON-safe shape stored in the Modal Dict."""
        return {
            "private_key_pem": self.private_key_pem.get_secret_value(),
            "certificate": self.certificate,
            "expires_at": self.expires_at.isoformat(),
            "role": self.role,
        }

    @classmethod
    def from_dict_entry(cls, entry: dict[str, str]) -> "SshCertificateBundle":
        return cls(
            private_key_pem=SecretStr(entry["private_key_pem"]),
            certificate=entry["certificate"],
            expires_at=datetime.fromisoformat(entry["expires_at"]),
            role=entry["role"],
        )


class ManagementSshCredentials(BaseModel):
    """What ``management_ssh_client`` authenticates with: a private key, and the certificate that goes with it on gen-2."""

    model_config = ConfigDict(frozen=True)

    private_key_pem: SecretStr = Field(description="OpenSSH ed25519 private key PEM")
    certificate: str | None = Field(
        description="The certificate presented instead of the raw key; None for the pool key"
    )

    def to_paramiko_key(self) -> paramiko.PKey:
        """The paramiko key, with the certificate attached so the server sees a certificate login."""
        private_key = paramiko.Ed25519Key.from_private_key(io.StringIO(self.private_key_pem.get_secret_value()))
        if self.certificate is not None:
            private_key.load_certificate(self.certificate)
        return private_key


def generate_ed25519_private_key_pem_and_public_key() -> tuple[SecretStr, str]:
    """A fresh ed25519 keypair as (OpenSSH private key PEM, OpenSSH public key line)."""
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_key_openssh = (
        private_key.public_key()
        .public_bytes(encoding=serialization.Encoding.OpenSSH, format=serialization.PublicFormat.OpenSSH)
        .decode()
    )
    return SecretStr(private_pem), public_key_openssh


class BundleSource(BaseModel):
    """Holder for the Dict reader the Modal entrypoint wires at import time (shipped modules never import modal)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # (dict key) -> the stored entry, or None when absent.
    reader: Callable[[str], dict[str, str] | None] | None = None
    cached_bundle: SshCertificateBundle | None = None
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)


bundle_source = BundleSource()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def gen2_management_credentials(source: BundleSource | None = None) -> ManagementSshCredentials:
    """The connector's current gen-2 certificate credentials, re-read from the Dict when the cached one nears expiry.

    ``source`` defaults to the process-wide :data:`bundle_source`; tests pass their own.

    Raises :class:`SshCertificateBundleMissingError` when the entrypoint wired
    no reader, the Dict holds no bundle, or the stored bundle is expired -- the
    SSH-bearing endpoints answer that as a 503 (the refresh cron, or the deploy
    that seeds it, has not run).
    """
    active_source = source if source is not None else bundle_source
    with active_source._lock:
        cached = active_source.cached_bundle
        if cached is not None and cached.is_fresh_at(_now()):
            return ManagementSshCredentials(private_key_pem=cached.private_key_pem, certificate=cached.certificate)
        if active_source.reader is None:
            raise SshCertificateBundleMissingError(
                "no management certificate source is configured in this process (the Modal entrypoint wires it)"
            )
        entry = active_source.reader(SSH_CERT_DICT_KEY_CONNECTOR)
        if entry is None:
            raise SshCertificateBundleMissingError(
                "no gen-2 management SSH certificate is available yet: the ssh_cert_refresh cron has not stored "
                "one (run `minds-admin env deploy`, which seeds it, and check the tier's ssh-ca secret)"
            )
        bundle = SshCertificateBundle.from_dict_entry(entry)
        if bundle.expires_at <= _now():
            raise SshCertificateBundleMissingError(
                f"the stored gen-2 management SSH certificate expired at {bundle.expires_at.isoformat()}; the "
                "ssh_cert_refresh cron has not refreshed it (is Vault reachable and the ssh-ca secret populated?)"
            )
        active_source.cached_bundle = bundle
        return ManagementSshCredentials(private_key_pem=bundle.private_key_pem, certificate=bundle.certificate)


def gen1_pool_management_credentials() -> ManagementSshCredentials:
    """The static pool key gen-1 boxes and VMs authorize.

    CLEANUP: drop with the ``pool-ssh`` secret once the gen-1 -> gen-2 cutover has
    run on every tier (phase 6 of blueprint/slice-fleet-cutover).
    """
    return ManagementSshCredentials(private_key_pem=SecretStr(os.environ["POOL_SSH_PRIVATE_KEY"]), certificate=None)


def management_credentials_for_generation(
    box_generation: int, source: BundleSource | None = None
) -> ManagementSshCredentials:
    """The credentials that open a box (and its VMs and containers) of ``box_generation``."""
    if box_generation >= FIRST_QEMU_BOX_GENERATION:
        return gen2_management_credentials(source)
    return gen1_pool_management_credentials()
