"""The operator's per-tier management identity: a WireGuard key and a Vault-certified SSH key.

Everything an operator machine holds to reach a tier's gen-2 boxes lives in
one directory per tier, ``~/.mindsadmin/<tier>/`` (``MINDS_ADMIN_IDENTITY_DIR``
overrides the root): the WireGuard private key the userspace tunnel dials
with, and an ed25519 SSH key whose certificate the tier's Vault SSH CA signs
(imbue-ai/mngr-internal#850). The certificate is short-lived and re-signed on
demand, so nothing in here is worth stealing for long and nothing is ever
authorized on a host: hosts trust the CA, not this key.

The management SSH identity is exposed as a private-key *path*: OpenSSH and
paramiko both pick the sibling ``<key>-cert.pub`` up on their own, so every
existing ``ssh -i`` / scp / rsync / pyinfra call site works unchanged.
"""

import fcntl
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.vault_reader import sign_ssh_public_key
from imbue.mngr.providers.ssh_utils import ssh_certificate_path_for
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_OPERATOR_PRINCIPALS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_VAULT_ROLE_OPERATOR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import ssh_ca_vault_mount

# The identity root; one subdirectory per tier below it. Deliberately not a
# ``~/.minds-<name>`` path: those are env roots (wiped by `env destroy`, one
# per env), while this material is per tier and per operator.
OPERATOR_IDENTITY_DIR_ENV_VAR: Final[str] = "MINDS_ADMIN_IDENTITY_DIR"
_DEFAULT_OPERATOR_IDENTITY_ROOT: Final[str] = "~/.mindsadmin"

WIREGUARD_KEY_FILENAME: Final[str] = "wireguard.key"
SSH_KEY_FILENAME: Final[str] = "ssh_id"
# Written beside the certificate at sign time; the certificate's own validity
# window is encoded in its wire format, and this sidecar saves decoding it.
_SSH_CERTIFICATE_META_SUFFIX: Final[str] = "-cert.json"

# The certificate lifetime asked of Vault (the role's max_ttl caps it) and the
# remaining validity below which a command re-signs before starting. A
# production bake with a cold image cache or a full `server setup` (OS
# reinstall plus prep) runs for hours, and the certificate must outlive the
# longest of them from whatever point the command started.
OPERATOR_CERTIFICATE_TTL: Final[timedelta] = timedelta(hours=12)
OPERATOR_CERTIFICATE_MIN_REMAINING: Final[timedelta] = timedelta(hours=6)

_SSH_KEYGEN_TIMEOUT_SECONDS: Final[float] = 30.0


class OperatorCertificateMeta(FrozenModel):
    """The sidecar recording when the operator's SSH certificate was signed and when it expires."""

    signed_at: datetime = Field(description="When Vault signed the certificate (UTC)")
    expires_at: datetime = Field(description="When the certificate stops being accepted (UTC)")
    tier: str = Field(description="The tier whose CA signed it")


def operator_identity_root() -> Path:
    """The identity root (``~/.mindsadmin`` unless :data:`OPERATOR_IDENTITY_DIR_ENV_VAR` points elsewhere); not created."""
    root_override = os.environ.get(OPERATOR_IDENTITY_DIR_ENV_VAR)
    return Path(root_override).expanduser() if root_override else Path(_DEFAULT_OPERATOR_IDENTITY_ROOT).expanduser()


def operator_identity_dir(tier: str) -> Path:
    """The per-tier identity directory (created 0700 on first use)."""
    identity_dir = operator_identity_root() / tier
    identity_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return identity_dir


def operator_wireguard_key_path(tier: str) -> Path:
    return operator_identity_dir(tier) / WIREGUARD_KEY_FILENAME


def operator_ssh_key_path(tier: str) -> Path:
    return operator_identity_dir(tier) / SSH_KEY_FILENAME


@pure
def is_certificate_fresh_enough(meta: OperatorCertificateMeta, now: datetime) -> bool:
    return meta.expires_at - now >= OPERATOR_CERTIFICATE_MIN_REMAINING


def _read_certificate_meta_or_none(private_key_path: Path) -> OperatorCertificateMeta | None:
    meta_path = private_key_path.with_name(private_key_path.name + _SSH_CERTIFICATE_META_SUFFIX)
    if not meta_path.is_file() or not ssh_certificate_path_for(private_key_path).is_file():
        return None
    try:
        return OperatorCertificateMeta.model_validate_json(meta_path.read_text())
    except ValueError as exc:
        logger.warning("Ignoring an unreadable certificate sidecar at {} ({}); re-signing", meta_path, exc)
        return None


def _ensure_operator_ssh_keypair(private_key_path: Path) -> None:
    """Generate the operator's ed25519 key on first use (``ssh-keygen``; no passphrase, 0600)."""
    if private_key_path.is_file() and private_key_path.with_name(private_key_path.name + ".pub").is_file():
        return
    cg = ConcurrencyGroup(name="mindsadmin-ssh-keygen")
    with cg:
        result = cg.run_process_to_completion(
            command=[
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                f"mindsadmin-{private_key_path.parent.name}",
                "-f",
                str(private_key_path),
            ],
            timeout=_SSH_KEYGEN_TIMEOUT_SECONDS,
            is_checked_after=False,
        )
    if result.returncode != 0:
        raise BareMetalProvisioningError(
            f"ssh-keygen failed to create the operator SSH key at {private_key_path}: {result.stderr.strip()}"
        )
    logger.info("Created the operator management SSH key at {}", private_key_path)


_IDENTITY_LOCK_SUFFIX: Final[str] = ".lock"


@contextmanager
def _locked_operator_identity(tier: str) -> Iterator[None]:
    """Serialize concurrent ``minds-admin`` invocations resolving the same tier's operator identity.

    Same pattern as ``mngr.providers.host_key_store``'s ``_locked_store``: an
    ``fcntl.flock`` on a sibling lock file, released when the file closes
    (including on an exception), so two operators (or CI jobs) signing at the
    same tier at once cannot both decide no fresh certificate exists and race
    to sign and write it.
    """
    lock_path = operator_ssh_key_path(tier).with_name(SSH_KEY_FILENAME + _IDENTITY_LOCK_SUFFIX)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield


def ensure_operator_ssh_identity(tier: str) -> Path:
    """Return the operator's certified SSH private key path for ``tier``, signing a fresh certificate when needed.

    Creates the key on first use, and asks the tier's Vault SSH CA (through
    the operator's own ``vault login``) for a certificate carrying every
    operator principal whenever none exists or the current one has less than
    :data:`OPERATOR_CERTIFICATE_MIN_REMAINING` left. Raises
    ``BareMetalProvisioningError`` when Vault refuses, with the login hint.
    """
    private_key_path = operator_ssh_key_path(tier)
    with _locked_operator_identity(tier):
        _ensure_operator_ssh_keypair(private_key_path)
        now = datetime.now(timezone.utc)
        meta = _read_certificate_meta_or_none(private_key_path)
        if meta is not None and meta.tier == tier and is_certificate_fresh_enough(meta, now):
            return private_key_path
        logger.info(
            "Signing a {}h management SSH certificate for tier '{}' with Vault",
            int(OPERATOR_CERTIFICATE_TTL.total_seconds() // 3600),
            tier,
        )
        try:
            certificate = sign_ssh_public_key(
                mount=ssh_ca_vault_mount(tier),
                role=SSH_CA_VAULT_ROLE_OPERATOR,
                public_key_path=private_key_path.with_name(private_key_path.name + ".pub"),
                ttl=f"{int(OPERATOR_CERTIFICATE_TTL.total_seconds())}s",
                principals=SSH_CA_OPERATOR_PRINCIPALS,
            )
        except VaultReadError as exc:
            raise BareMetalProvisioningError(
                f"could not sign the operator's management SSH certificate for tier '{tier}': {exc}. Run "
                f"`vault login -method=oidc` (with the tier's role for staging/production) and retry."
            ) from exc
        certificate_path = ssh_certificate_path_for(private_key_path)
        atomic_write(certificate_path, certificate + "\n")
        certificate_path.chmod(0o644)
        meta_path = private_key_path.with_name(private_key_path.name + _SSH_CERTIFICATE_META_SUFFIX)
        new_meta = OperatorCertificateMeta(signed_at=now, expires_at=now + OPERATOR_CERTIFICATE_TTL, tier=tier)
        atomic_write(meta_path, json.dumps(new_meta.model_dump(mode="json")) + "\n")
        meta_path.chmod(0o600)
        return private_key_path


POOL_KEY_FILENAME: Final[str] = "id"


def write_pool_private_key_dir(private_key_pem: str) -> Path:
    """Write the gen-1 pool management private key PEM into a fresh 0700 temp dir as a 0600 file; return the dir.

    CLEANUP: drop with the gen-1 pool key once the gen-1 -> gen-2 cutover has run on
    every tier (phase 6 of blueprint/slice-fleet-cutover).
    """
    key_dir = Path(tempfile.mkdtemp(prefix="mngr-pool-key-"))
    key_path = key_dir / POOL_KEY_FILENAME
    key_path.write_text(private_key_pem if private_key_pem.endswith("\n") else private_key_pem + "\n")
    key_path.chmod(0o600)
    return key_dir


@contextmanager
def pool_private_key_path(private_key_pem: str) -> Iterator[Path]:
    """Yield a 0600 temp file holding the gen-1 pool management private key PEM; removed on exit."""
    key_dir = write_pool_private_key_dir(private_key_pem)
    try:
        yield key_dir / POOL_KEY_FILENAME
    finally:
        shutil.rmtree(key_dir, ignore_errors=True)


class ManagementIdentityResolver(MutableModel):
    """Which private key opens a box for one command: the certified operator key on gen-2, the tier's pool key on gen-1.

    Both are resolved lazily and at most once per command: the certificate only
    when a gen-2 box is dialed (a sign is a Vault round trip), the pool key only
    when a gen-1 box is (a Vault read into a 0600 temp file that
    :func:`management_identities` removes on exit). A single resolver is shared
    across a command's worker threads (e.g. a parallel pool-host destroy), so
    each resource is guarded by its own lock: a thread that loses the race
    blocks on the lock and reuses the winner's result instead of racing the
    None-check-then-set and triggering a duplicate Vault sign or leaking a
    second pool-key temp dir.
    """

    tier: str | None = Field(
        frozen=True,
        description="The activated tier, or None outside an activated env (where only gen-1 boxes can be dialed)",
    )
    resolve_gen1_pool_private_key_pem: Callable[[], str] = Field(
        frozen=True, description="Resolves the tier's static pool key PEM (Vault, or the env-var override)"
    )
    gen1_key_dir: Path | None = Field(default=None, description="Temp dir holding the gen-1 pool key, once created")
    operator_key_path: Path | None = Field(
        default=None, description="The certified operator key, once resolved (tests preset it to skip Vault)"
    )
    _operator_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _gen1_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def private_key_path_for(self, box_generation: int) -> Path:
        if box_generation >= FIRST_QEMU_BOX_GENERATION:
            with self._operator_lock:
                if self.operator_key_path is None:
                    if self.tier is None:
                        raise BareMetalProvisioningError(
                            "a gen-2 box is reached with a Vault-signed SSH certificate for the tier's CA, which "
                            "needs an activated minds env: run `minds-admin env activate <env>` first"
                        )
                    self.operator_key_path = ensure_operator_ssh_identity(self.tier)
                return self.operator_key_path
        with self._gen1_lock:
            if self.gen1_key_dir is None:
                self.gen1_key_dir = write_pool_private_key_dir(self.resolve_gen1_pool_private_key_pem())
            return self.gen1_key_dir / POOL_KEY_FILENAME

    def close(self) -> None:
        if self.gen1_key_dir is not None:
            shutil.rmtree(self.gen1_key_dir, ignore_errors=True)
            self.gen1_key_dir = None


@contextmanager
def management_identities(
    *,
    tier: str | None,
    resolve_gen1_pool_private_key_pem: Callable[[], str],
) -> Iterator[ManagementIdentityResolver]:
    """A per-command resolver of box management keys; removes the gen-1 pool key temp file on exit."""
    resolver = ManagementIdentityResolver(
        tier=tier, resolve_gen1_pool_private_key_pem=resolve_gen1_pool_private_key_pem
    )
    try:
        yield resolver
    finally:
        resolver.close()
