"""R2 bucket naming and ownership conventions."""

import hashlib
import re
from typing import Final

from imbue.remote_service_connector.errors import InvalidR2BucketNameError
from imbue.remote_service_connector.errors import R2BucketOwnershipError

R2_BUCKET_NAME_SEP = "--"
_R2_BUCKET_MIN_LENGTH = 3
_R2_BUCKET_MAX_LENGTH = 63
_R2_BUCKET_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
DEFAULT_R2_KEY_ALIAS = "default"


def slugify_r2_name(value: str) -> str:
    """Lowercase + collapse non-alphanumeric runs into single hyphens; strip edge hyphens."""
    lowered = value.strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")


def _validate_r2_bucket_name(name: str) -> None:
    if not (_R2_BUCKET_MIN_LENGTH <= len(name) <= _R2_BUCKET_MAX_LENGTH) or not _R2_BUCKET_NAME_RE.match(name):
        raise InvalidR2BucketNameError(name)


def bucket_owner_prefix(user_id_prefix: str) -> str:
    return f"{user_id_prefix}{R2_BUCKET_NAME_SEP}"


def make_bucket_name(user_id_prefix: str, short_name: str) -> str:
    """Derive the full R2 bucket name from the owner prefix and the user's short name."""
    name = f"{bucket_owner_prefix(user_id_prefix)}{slugify_r2_name(short_name)}"
    _validate_r2_bucket_name(name)
    return name


def verify_bucket_ownership(bucket_name: str, user_id_prefix: str) -> None:
    if not bucket_name.startswith(bucket_owner_prefix(user_id_prefix)):
        raise R2BucketOwnershipError(bucket_name, user_id_prefix)


# How long a destroyed workspace's backup (bucket + record) is retained before
# the reapers delete it permanently. Served to clients via
# GET /policies/destroyed-workspace-backups; a fixed constant for now (per-plan
# retention is an explicit future option).
DESTROYED_WORKSPACE_BACKUP_RETENTION_SECONDS: Final[float] = 60.0 * 60.0 * 24.0 * 30.0

# Workspace-backup buckets are named by their workspace's host id, so the
# short name after the owner prefix is always `host-<hex>`. This shape is
# reserved: `bucket create` refuses it for names no workspace record backs,
# so the reapers can identify workspace-backup buckets by name alone.
WORKSPACE_BACKUP_SHORT_NAME_RE: Final = re.compile(r"^host-[a-f0-9]+$")
RESERVED_BUCKET_SHORT_NAME_PREFIX: Final[str] = "host-"


def parse_workspace_backup_bucket_name(bucket_name: str) -> tuple[str, str] | None:
    """Split a full bucket name into (user_id_prefix, host_id) when it is a workspace-backup bucket."""
    if R2_BUCKET_NAME_SEP not in bucket_name:
        return None
    user_id_prefix, _, short_name = bucket_name.partition(R2_BUCKET_NAME_SEP)
    if not user_id_prefix or not WORKSPACE_BACKUP_SHORT_NAME_RE.match(short_name):
        return None
    return user_id_prefix, short_name


def r2_s3_endpoint(account_id: str) -> str:
    return f"https://{account_id}.r2.cloudflarestorage.com"


def derive_s3_secret_access_key(token_value: str) -> str:
    """R2 derives the S3 Secret Access Key as the SHA-256 hex digest of the API token value."""
    return hashlib.sha256(token_value.encode()).hexdigest()


def r2_token_name(bucket_name: str, alias: str | None) -> str:
    return f"mngr-r2:{bucket_name}:{alias or DEFAULT_R2_KEY_ALIAS}"
