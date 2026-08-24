"""R2 bucket + bucket-key endpoints and helpers."""

import concurrent.futures
import logging
from typing import Any
from typing import Final

import httpx
import psycopg2
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

import imbue.remote_service_connector.accounts_web as accounts_web_module
import imbue.remote_service_connector.cloudflare as cloudflare_module
import imbue.remote_service_connector.entitlements as entitlements_module
import imbue.remote_service_connector.r2.stores as stores_module
from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector import db
from imbue.remote_service_connector.cloudflare import CloudflareOps
from imbue.remote_service_connector.entitlements import AccountEntitlements
from imbue.remote_service_connector.entitlements import raise_quota_exceeded
from imbue.remote_service_connector.errors import CloudflareApiError
from imbue.remote_service_connector.errors import InvalidR2AccessError
from imbue.remote_service_connector.errors import R2BucketActiveWorkspaceError
from imbue.remote_service_connector.errors import R2BucketExistsError
from imbue.remote_service_connector.errors import R2BucketNotEmptyError
from imbue.remote_service_connector.errors import R2BucketNotFoundError
from imbue.remote_service_connector.errors import R2ReservedBucketNameError
from imbue.remote_service_connector.http_api import handle_endpoint_errors
from imbue.remote_service_connector.r2.naming import DEFAULT_R2_KEY_ALIAS
from imbue.remote_service_connector.r2.naming import RESERVED_BUCKET_SHORT_NAME_PREFIX
from imbue.remote_service_connector.r2.naming import WORKSPACE_BACKUP_SHORT_NAME_RE
from imbue.remote_service_connector.r2.naming import bucket_owner_prefix
from imbue.remote_service_connector.r2.naming import derive_s3_secret_access_key
from imbue.remote_service_connector.r2.naming import make_bucket_name
from imbue.remote_service_connector.r2.naming import r2_s3_endpoint
from imbue.remote_service_connector.r2.naming import r2_token_name
from imbue.remote_service_connector.r2.naming import slugify_r2_name
from imbue.remote_service_connector.r2.naming import verify_bucket_ownership
from imbue.remote_service_connector.r2.stores import KeyStore

logger = logging.getLogger(__name__)

router = APIRouter()


_R2_ACCESS_VALUES = ("read", "readwrite")


def _validate_r2_access(value: str) -> str:
    """Field validator: constrain the per-key access scope to read/readwrite."""
    if value not in _R2_ACCESS_VALUES:
        raise InvalidR2AccessError(value)
    return value


class CreateBucketRequest(BaseModel):
    name: str = Field(description="User's short bucket name (the server prefixes it with the owner id)")
    access: str = Field(default="readwrite", description="Access scope for the default key: 'read' or 'readwrite'")

    _validate_access = field_validator("access")(_validate_r2_access)


class BucketInfo(BaseModel):
    bucket_name: str = Field(description="Full R2 bucket name (<user_id_prefix>--<slug>)")
    s3_endpoint: str = Field(description="S3-compatible endpoint for this account")


class R2KeyMaterial(BaseModel):
    access_key_id: str = Field(description="S3 Access Key ID (= the Cloudflare token id)")
    secret_access_key: str = Field(description="S3 Secret Access Key (sha256 of the token value); shown once")
    s3_endpoint: str = Field(description="S3-compatible endpoint for this account")
    bucket_name: str = Field(description="Full R2 bucket name this key is scoped to")
    access: str = Field(description="Access scope: 'read' or 'readwrite'")


class CreateBucketResponse(BaseModel):
    bucket: BucketInfo = Field(description="The created bucket")
    key: R2KeyMaterial = Field(description="The default key minted alongside the bucket")


class R2KeyInfo(BaseModel):
    access_key_id: str = Field(description="S3 Access Key ID (= the Cloudflare token id)")
    bucket_name: str = Field(description="Full R2 bucket name this key is scoped to")
    access: str = Field(description="Access scope: 'read' or 'readwrite'")
    alias: str | None = Field(default=None, description="Human-readable alias")
    created_at: str = Field(description="ISO 8601 timestamp when the key was created")
    enforced_access: str | None = Field(
        default=None,
        description=(
            "Storage-quota enforcement state: 'read' when the sweep downgraded this key because the "
            "owner is over their storage quota; None when the live token policy matches ``access``."
        ),
    )


class CleanupGrantResponse(BaseModel):
    """Result of a storage-cleanup-grant request."""

    status: str = Field(description="'granted' when a grant is active (new or pre-existing), 'not_needed' otherwise")
    expires_at: str | None = Field(default=None, description="When the active grant expires (settlement fallback)")
    baseline_bytes: int | None = Field(default=None, description="Live usage recorded at grant time")
    keys: list[R2KeyInfo] = Field(description="The caller's bucket keys after the grant was applied")


class StorageRecheckResponse(BaseModel):
    """Result of an on-demand storage-enforcement recheck."""

    usage_bytes: int = Field(description="Live total bucket bytes (real-time REST usage)")
    limit_bytes: int = Field(description="The account's max_total_bucket_bytes entitlement")
    is_over_quota: bool = Field(description="Whether live usage exceeds the limit")
    is_grant_settled: bool = Field(description="Whether this recheck settled an outstanding cleanup grant")
    keys: list[R2KeyInfo] = Field(description="The caller's bucket keys after enforcement was applied")


def list_owned_buckets(ops: CloudflareOps, user_id_prefix: str) -> list[dict[str, Any]]:
    """List the caller's buckets: R2 name_contains filter, then re-verify the prefix in code."""
    prefix = bucket_owner_prefix(user_id_prefix)
    return [b for b in ops.list_buckets(name_contains=prefix) if str(b.get("name", "")).startswith(prefix)]


def _owned_bucket_exists(ops: CloudflareOps, user_id_prefix: str, full_name: str) -> bool:
    return any(b.get("name") == full_name for b in list_owned_buckets(ops, user_id_prefix))


# Bound on simultaneous per-bucket usage REST calls. Reads were previously
# sequential, which made every live-usage measurement O(bucket_count) in
# Cloudflare round trips (~0.45s each -- ~19s for a 42-bucket account).
_BUCKET_USAGE_MAX_PARALLEL_READS: Final = 8


def _read_one_bucket_usage_bytes(ops: CloudflareOps, bucket_name: str) -> int | CloudflareApiError | httpx.HTTPError:
    """Read one bucket's live usage bytes, returning (not raising) a failed read's exception."""
    try:
        return ops.get_bucket_usage_bytes(bucket_name)
    except (CloudflareApiError, httpx.HTTPError) as exc:
        return exc


def read_bucket_usage_bytes_concurrently(
    ops: CloudflareOps, bucket_names: list[str]
) -> list[int | CloudflareApiError | httpx.HTTPError]:
    """Read each bucket's live usage bytes via concurrent REST calls.

    Results align positionally with ``bucket_names``. A failed read yields its
    exception instead of raising, so each caller keeps its own error
    semantics (display warns and counts zero; enforcement raises).
    """
    if not bucket_names:
        return []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(_BUCKET_USAGE_MAX_PARALLEL_READS, len(bucket_names))
    ) as pool:
        futures = [pool.submit(_read_one_bucket_usage_bytes, ops, bucket_name) for bucket_name in bucket_names]
        return [future.result() for future in futures]


def measure_live_owner_usage_bytes(ops: CloudflareOps, user_id_prefix: str) -> int:
    """Sum the owner's bucket bytes via the real-time REST usage endpoint.

    Raises :class:`CloudflareApiError` / ``httpx.HTTPError`` on any failed
    read -- callers decide whether that fails open (sweep, creation gate) or
    fails the request (grant baseline, recheck).
    """
    bucket_names = [str(bucket.get("name", "")) for bucket in list_owned_buckets(ops, user_id_prefix)]
    total_bytes = 0
    for result in read_bucket_usage_bytes_concurrently(ops, bucket_names):
        if isinstance(result, (CloudflareApiError, httpx.HTTPError)):
            raise result
        total_bytes += result
    return total_bytes


def _is_owner_enforced_over_quota(store: KeyStore, owner_user_id: str) -> bool:
    """True when any of the owner's keys is currently sweep-downgraded (enforced read-only)."""
    return any(row.get("enforced_access") == "read" for row in store.list_keys(owner_user_id, None))


def _check_storage_quota_for_new_bucket(
    ops: CloudflareOps, user_id_prefix: str, entitlements: AccountEntitlements
) -> None:
    """Refuse bucket creation when the owner's live storage usage is already over quota.

    A failed usage read fails open (creation proceeds with a warning),
    consistent with the sweep's missing-data-never-downgrades rule.
    """
    try:
        live_bytes = measure_live_owner_usage_bytes(ops, user_id_prefix)
    except (CloudflareApiError, httpx.HTTPError) as exc:
        # Fail-open on an enforcement decision: worth a low-priority report,
        # and the metric's rate shows whether failing open is becoming routine.
        emit_metric("cloudflare_api_failed", 1, {"operation": "bucket_creation_quota_check"})
        logger.warning("Skipped the storage-quota check for bucket creation (usage read failed)", exc_info=exc)
        return
    if live_bytes > entitlements.max_total_bucket_bytes:
        raise_quota_exceeded(
            "max_total_bucket_bytes", entitlements.max_total_bucket_bytes, live_bytes, "bytes of bucket storage"
        )


def best_effort_revoke_token(ops: CloudflareOps, token_id: str) -> None:
    try:
        ops.delete_bucket_token(token_id)
    except (CloudflareApiError, httpx.HTTPError) as exc:
        emit_metric("cloudflare_api_failed", 1, {"operation": "best_effort_revoke_token"})
        logger.warning("Failed to revoke R2 token %s", token_id, exc_info=exc)


def _best_effort_delete_bucket(ops: CloudflareOps, bucket_name: str) -> None:
    try:
        ops.delete_bucket(bucket_name)
    except (CloudflareApiError, R2BucketNotEmptyError, R2BucketNotFoundError, httpx.HTTPError) as exc:
        emit_metric("cloudflare_api_failed", 1, {"operation": "bucket_rollback_delete"})
        logger.warning("Failed to roll back bucket %s", bucket_name, exc_info=exc)


def key_info_from_row(row: dict[str, Any]) -> R2KeyInfo:
    return R2KeyInfo(
        access_key_id=row["access_key_id"],
        bucket_name=row["bucket_name"],
        access=row["access"],
        alias=row["alias"],
        created_at=row["created_at"],
        enforced_access=row.get("enforced_access"),
    )


def _mint_and_record_key(
    ops: CloudflareOps,
    store: KeyStore,
    owner_user_id: str,
    bucket_name: str,
    access: str,
    alias: str | None,
    rollback_bucket: bool,
    # When the owner is currently enforced-over-quota, a readwrite key is
    # minted with a read-only token policy and recorded as enforced -- a
    # fresh mint must not hand out a writable key the sweep already denies.
    is_enforced_read: bool,
) -> R2KeyMaterial:
    """Mint a bucket-scoped Cloudflare token, record its metadata, and return the S3 material.

    On any failure, best-effort revokes a partially-created token and (when
    ``rollback_bucket``) deletes the just-created bucket so ``bucket create``
    stays atomic.
    """
    minted_access = "read" if is_enforced_read and access == "readwrite" else access
    created_token_id: str | None = None
    try:
        token_result = ops.create_bucket_token(bucket_name, minted_access, r2_token_name(bucket_name, alias))
        access_key_id = str(token_result["id"])
        created_token_id = access_key_id
        secret_access_key = derive_s3_secret_access_key(str(token_result["value"]))
        store.add_key(access_key_id, owner_user_id, bucket_name, access, alias)
        if minted_access != access:
            store.set_enforced_access(access_key_id, "read")
        return R2KeyMaterial(
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            s3_endpoint=r2_s3_endpoint(ops.account_id),
            bucket_name=bucket_name,
            access=minted_access,
        )
    except (CloudflareApiError, httpx.HTTPError, psycopg2.Error) as exc:
        if created_token_id is not None:
            best_effort_revoke_token(ops, created_token_id)
        if rollback_bucket:
            _best_effort_delete_bucket(ops, bucket_name)
        raise HTTPException(status_code=502, detail=f"Failed to provision bucket key: {exc}") from exc


def _workspace_record_exists(user_id: str, host_id: str) -> bool:
    """Whether the user has a workspace record (any state) for ``host_id``."""
    conn = db.get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM workspace_records WHERE user_id = %s AND host_id = %s",
                (user_id, host_id),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def _workspace_record_is_active(user_id: str, host_id: str) -> bool:
    """Whether the user has an ACTIVE workspace record for ``host_id``."""
    conn = db.get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM workspace_records WHERE user_id = %s AND host_id = %s AND state = 'active'",
                (user_id, host_id),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


@router.post("/buckets")
def create_bucket_endpoint(request: Request, body: CreateBucketRequest) -> dict[str, object]:
    """Create an R2 bucket for the caller and mint its single key (returned inline)."""
    with handle_endpoint_errors():
        user, owner_user_id = accounts_web_module.resolve_web_user_identity(request)
        entitlements = entitlements_module.resolve_entitlements_for_user(owner_user_id, user)
        ops = cloudflare_module.get_cloudflare_ctx().ops
        full_name = make_bucket_name(user.user_id_prefix, body.name)
        # The `host-` short-name shape is reserved for workspace-backup buckets:
        # allowed only when the caller has a workspace record (any state) with
        # that host id, so generic user buckets can never collide with the
        # names the backup reapers act on. The check runs on the slugified
        # short name -- the name the bucket is actually created under -- so
        # case/punctuation variants (e.g. 'HOST-abc') cannot slip into the
        # reserved namespace.
        short_name = slugify_r2_name(body.name)
        if short_name.startswith(RESERVED_BUCKET_SHORT_NAME_PREFIX) and not _workspace_record_exists(
            owner_user_id, short_name
        ):
            raise R2ReservedBucketNameError(short_name)
        owned = list_owned_buckets(ops, user.user_id_prefix)
        if any(b.get("name") == full_name for b in owned):
            raise R2BucketExistsError(full_name)
        if len(owned) >= entitlements.max_buckets:
            raise_quota_exceeded("max_buckets", entitlements.max_buckets, len(owned), "buckets")
        _check_storage_quota_for_new_bucket(ops, user.user_id_prefix, entitlements)
        store = stores_module.get_key_store()
        ops.create_bucket(full_name)
        material = _mint_and_record_key(
            ops,
            store,
            owner_user_id,
            full_name,
            body.access,
            DEFAULT_R2_KEY_ALIAS,
            rollback_bucket=True,
            is_enforced_read=_is_owner_enforced_over_quota(store, owner_user_id),
        )
        return CreateBucketResponse(
            bucket=BucketInfo(bucket_name=full_name, s3_endpoint=r2_s3_endpoint(ops.account_id)),
            key=material,
        ).model_dump()


@router.get("/buckets")
def list_buckets_endpoint(request: Request) -> list[dict[str, object]]:
    """List all R2 buckets owned by the caller."""
    with handle_endpoint_errors():
        user = accounts_web_module.authenticate_web_request(request)
        ops = cloudflare_module.get_cloudflare_ctx().ops
        endpoint = r2_s3_endpoint(ops.account_id)
        return [
            BucketInfo(bucket_name=str(b["name"]), s3_endpoint=endpoint).model_dump()
            for b in list_owned_buckets(ops, user.user_id_prefix)
        ]


@router.get("/buckets/{name}")
def get_bucket_endpoint(request: Request, name: str) -> dict[str, object]:
    """Return metadata for one of the caller's buckets (keys come from the keys endpoints)."""
    with handle_endpoint_errors():
        user = accounts_web_module.authenticate_web_request(request)
        ops = cloudflare_module.get_cloudflare_ctx().ops
        full_name = make_bucket_name(user.user_id_prefix, name)
        if not _owned_bucket_exists(ops, user.user_id_prefix, full_name):
            raise R2BucketNotFoundError(full_name)
        return BucketInfo(bucket_name=full_name, s3_endpoint=r2_s3_endpoint(ops.account_id)).model_dump()


@router.delete("/buckets/{name}")
def delete_bucket_endpoint(request: Request, name: str) -> dict[str, str]:
    """Destroy one of the caller's buckets (refuses non-empty) and cascade-revoke its keys.

    A workspace-backup bucket (`host-<hex>` short name) whose workspace record
    is still ACTIVE is refused -- tombstone-first is enforced server-side so a
    live workspace's backups can never be deleted.
    """
    with handle_endpoint_errors():
        user, owner_user_id = accounts_web_module.resolve_web_user_identity(request)
        ops = cloudflare_module.get_cloudflare_ctx().ops
        full_name = make_bucket_name(user.user_id_prefix, name)
        verify_bucket_ownership(full_name, user.user_id_prefix)
        # The interlock runs on the slugified short name -- the name the
        # bucket is actually deleted under -- so case variants of the path
        # parameter cannot bypass it.
        short_name = slugify_r2_name(name)
        if WORKSPACE_BACKUP_SHORT_NAME_RE.match(short_name) and _workspace_record_is_active(owner_user_id, short_name):
            raise R2BucketActiveWorkspaceError(full_name, short_name)
        ops.delete_bucket(full_name)
        revoked = stores_module.get_key_store().delete_keys_for_bucket(owner_user_id, full_name)
        for row in revoked:
            best_effort_revoke_token(ops, str(row["access_key_id"]))
        return {"status": "deleted"}


@router.post("/buckets/{name}/roll-key")
def roll_bucket_key_endpoint(request: Request, name: str) -> dict[str, object]:
    """Return fresh credentials for a bucket's single key by rolling its secret in place.

    Each bucket has exactly one key. The secret is derived from the
    Cloudflare token value and is shown only once, so re-provisioning
    (e.g. minds re-applying backups) rolls the existing token's value --
    same Access Key ID, fresh Secret Access Key, and, crucially, the
    token's *policies* are untouched, so a storage-quota downgrade
    survives a roll. When the bucket has no recorded key (revoked, or
    a legacy bucket), a fresh key is minted instead.
    """
    with handle_endpoint_errors():
        user, owner_user_id = accounts_web_module.resolve_web_user_identity(request)
        ops = cloudflare_module.get_cloudflare_ctx().ops
        full_name = make_bucket_name(user.user_id_prefix, name)
        if not _owned_bucket_exists(ops, user.user_id_prefix, full_name):
            raise R2BucketNotFoundError(full_name)
        store = stores_module.get_key_store()
        rows = store.list_keys(owner_user_id, full_name)
        if not rows:
            material = _mint_and_record_key(
                ops,
                store,
                owner_user_id,
                full_name,
                "readwrite",
                DEFAULT_R2_KEY_ALIAS,
                rollback_bucket=False,
                is_enforced_read=_is_owner_enforced_over_quota(store, owner_user_id),
            )
            return material.model_dump()
        # The sweep enforces single-key-per-bucket; if extras still exist
        # (pre-sweep), roll the newest -- the sweep will revoke the rest.
        newest = rows[-1]
        result = ops.roll_bucket_token_value(str(newest["access_key_id"]))
        secret_access_key = derive_s3_secret_access_key(str(result["value"]))
        effective_access = str(newest.get("enforced_access") or newest["access"])
        return R2KeyMaterial(
            access_key_id=str(newest["access_key_id"]),
            secret_access_key=secret_access_key,
            s3_endpoint=r2_s3_endpoint(ops.account_id),
            bucket_name=full_name,
            access=effective_access,
        ).model_dump()


@router.get("/buckets/{name}/keys")
def list_bucket_keys_endpoint(request: Request, name: str) -> list[dict[str, object]]:
    """List the caller's keys scoped to one bucket."""
    with handle_endpoint_errors():
        user, owner_user_id = accounts_web_module.resolve_web_user_identity(request)
        full_name = make_bucket_name(user.user_id_prefix, name)
        rows = stores_module.get_key_store().list_keys(owner_user_id, full_name)
        return [key_info_from_row(row).model_dump() for row in rows]


@router.get("/bucket-keys")
def list_all_bucket_keys_endpoint(request: Request) -> list[dict[str, object]]:
    """List all of the caller's bucket keys across every bucket."""
    with handle_endpoint_errors():
        owner_user_id = accounts_web_module.resolve_web_user_identity(request)[1]
        rows = stores_module.get_key_store().list_keys(owner_user_id, None)
        return [key_info_from_row(row).model_dump() for row in rows]


@router.delete("/bucket-keys/{access_key_id}")
def delete_bucket_key_endpoint(request: Request, access_key_id: str) -> dict[str, str]:
    """Revoke one of the caller's bucket keys (by Access Key ID) and drop its DB row."""
    with handle_endpoint_errors():
        owner_user_id = accounts_web_module.resolve_web_user_identity(request)[1]
        store = stores_module.get_key_store()
        row = store.get_key(access_key_id)
        if row is None or row["owner_user_id"] != owner_user_id:
            raise HTTPException(status_code=404, detail="Key not found")
        cloudflare_module.get_cloudflare_ctx().ops.delete_bucket_token(access_key_id)
        store.delete_key(access_key_id)
        return {"status": "deleted"}
