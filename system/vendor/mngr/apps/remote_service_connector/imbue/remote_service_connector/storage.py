"""Tier object-storage config + helpers for workspace stop/start artifacts.

A stopped workspace's disks live as encrypted objects in the tier's OVH
S3-compatible bucket. The boxes do the actual byte transfer (s5cmd, launched
by the transition supervisor); this module owns the connector-side pieces:
the ``storage-<env>`` secret's config, envelope-encryption of the per-stop
age identity under the tier KEK, and object deletion (destroy, generation
cleanup) over the S3 API.
"""

import base64
import logging
import os
import secrets
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel
from pydantic import Field

from imbue.remote_service_connector.errors import MissingStorageConfigError
from imbue.remote_service_connector.errors import StorageDeletionError

logger = logging.getLogger(__name__)

# AESGCM nonce length for the wrapped-DEK envelope (standard 96-bit nonce).
_WRAP_NONCE_LENGTH = 12


class StorageConfig(BaseModel):
    """The tier's workspace-artifact storage settings, read from the storage-<env> secret."""

    s3_endpoint: str = Field(description="S3-compatible endpoint URL (OVH Object Storage)")
    s3_region: str = Field(description="S3 region name (e.g. us-east-va)")
    access_key_id: str = Field(description="S3 access key id")
    secret_access_key: str = Field(description="S3 secret access key")
    bucket: str = Field(description="Bucket holding every workspace artifact for this env")
    kek_base64: str = Field(description="Base64 32-byte tier key that wraps per-stop age identities")
    retention_seconds: int = Field(description="Local-retention window after stop before the slot is freed")
    key_prefix: str = Field(
        default="",
        description=(
            "Optional object-key prefix namespacing this env inside a tier-shared bucket "
            "(e.g. 'dev-alice/'). Empty for tiers with a dedicated bucket."
        ),
    )


def read_storage_config() -> StorageConfig:
    """Read the storage config from the environment, raising if any piece is missing.

    Raises :class:`MissingStorageConfigError` naming the first missing
    variable, which the HTTP layer maps to a 503 -- a tier without storage
    configured cleanly refuses stop/start instead of half-working.
    """
    values: dict[str, str] = {}
    for name in (
        "WORKSPACE_STORAGE_S3_ENDPOINT",
        "WORKSPACE_STORAGE_S3_REGION",
        "WORKSPACE_STORAGE_S3_ACCESS_KEY",
        "WORKSPACE_STORAGE_S3_SECRET_KEY",
        "WORKSPACE_STORAGE_BUCKET",
        "WORKSPACE_STORAGE_KEK",
    ):
        value = os.environ.get(name, "")
        if not value:
            raise MissingStorageConfigError(name)
        values[name] = value
    return StorageConfig(
        s3_endpoint=values["WORKSPACE_STORAGE_S3_ENDPOINT"],
        s3_region=values["WORKSPACE_STORAGE_S3_REGION"],
        access_key_id=values["WORKSPACE_STORAGE_S3_ACCESS_KEY"],
        secret_access_key=values["WORKSPACE_STORAGE_S3_SECRET_KEY"],
        bucket=values["WORKSPACE_STORAGE_BUCKET"],
        kek_base64=values["WORKSPACE_STORAGE_KEK"],
        # The Vault template ships this optional line as an empty export, so
        # an empty value must fall back to the default too.
        retention_seconds=int(os.environ.get("WORKSPACE_STOP_RETENTION_SECONDS") or "3600"),
        key_prefix=os.environ.get("WORKSPACE_STORAGE_KEY_PREFIX", ""),
    )


def is_storage_configured() -> bool:
    return bool(os.environ.get("WORKSPACE_STORAGE_BUCKET"))


def workspace_key_prefix(config: StorageConfig, mngr_host_id: str) -> str:
    """The object-key prefix holding every artifact object for one workspace."""
    return f"{config.key_prefix}{mngr_host_id}"


def make_s3_client(config: StorageConfig) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=config.s3_endpoint,
        region_name=config.s3_region,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
    )


def wrap_dek(config: StorageConfig, identity: str) -> str:
    """Encrypt the per-stop age identity under the tier KEK (base64 nonce+ciphertext)."""
    kek = base64.b64decode(config.kek_base64)
    nonce = secrets.token_bytes(_WRAP_NONCE_LENGTH)
    ciphertext = AESGCM(kek).encrypt(nonce, identity.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def unwrap_dek(config: StorageConfig, wrapped: str) -> str:
    """Decrypt a wrapped age identity back to its plaintext string."""
    kek = base64.b64decode(config.kek_base64)
    blob = base64.b64decode(wrapped)
    nonce, ciphertext = blob[:_WRAP_NONCE_LENGTH], blob[_WRAP_NONCE_LENGTH:]
    plaintext = AESGCM(kek).decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")


def delete_prefix(config: StorageConfig, prefix: str) -> int:
    """Delete every object under ``prefix`` in the tier bucket; return the count deleted.

    Used by destroy (drop the whole workspace's artifacts), by re-stop
    (drop the superseded generation), and by start (drop a partial upload
    after an in-window restart). Idempotent: a missing prefix deletes 0.
    Raises :class:`StorageDeletionError` on any S3 failure so the enclosing
    release/transition stays retryable instead of reporting a false success.
    """
    client = make_s3_client(config)
    return _delete_prefix_with_client(client, config.bucket, prefix)


# Keep in sync with ``imbue.minds_admin.envs.providers.workspace_storage`` -- the
# private minds-admin env tooling carries its own copy of this loop because the
# shipped connector container cannot import it.
def _delete_prefix_with_client(client: Any, bucket: str, prefix: str) -> int:
    deleted_count = 0
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            keys = [{"Key": entry["Key"]} for entry in page.get("Contents", [])]
            if not keys:
                continue
            response = client.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
            # Quiet mode reports per-key failures in ``Errors`` rather than
            # raising, so surface them explicitly: a partially-deleted prefix
            # must fail the caller (keeping it retryable), not read as fully
            # reclaimed while orphaning the remaining objects.
            errors = response.get("Errors", [])
            if errors:
                failed_keys = ", ".join(str(error.get("Key")) for error in errors[:5])
                raise StorageDeletionError(
                    f"failed to delete {len(errors)} artifact object(s) under {bucket}/{prefix} (e.g. {failed_keys})"
                )
            deleted_count += len(keys)
    except (ClientError, BotoCoreError) as e:
        raise StorageDeletionError(f"failed to delete artifact objects under {bucket}/{prefix}: {e}") from e
    if deleted_count:
        logger.info("Deleted %d artifact objects under %s", deleted_count, prefix)
    return deleted_count
