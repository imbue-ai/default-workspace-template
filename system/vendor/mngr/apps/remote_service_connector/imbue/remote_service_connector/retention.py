"""Destroyed-workspace backup retention reaper (hourly cron body + admin sweeps).

The server-side backstop for the 30-day backup retention policy: destroyed
workspace records past the window lose their backup bucket and then the
record itself; workspace-backup buckets no record references at all
(orphans) age from a first-seen stamp and are then reaped too. Emptying is
bounded per pass and resumable -- a partially-emptied bucket continues on
the next pass, and the bucket + record deletion lands on the pass that
finishes -- so a single cron invocation never runs long. The client-side
reaper in minds does the same work faster where a client is running; every
step here is idempotent so the two never conflict.
"""

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Final

from fastapi import APIRouter
from fastapi import Request

import imbue.remote_service_connector.accounts as accounts_module
import imbue.remote_service_connector.cloudflare as cloudflare_module
import imbue.remote_service_connector.entitlements as entitlements_module
import imbue.remote_service_connector.r2.stores as r2_stores_module
import imbue.remote_service_connector.sync as sync_module
from imbue.remote_service_connector.auth import derive_user_id_prefix
from imbue.remote_service_connector.auth import require_admin_key
from imbue.remote_service_connector.cloudflare import CloudflareOps
from imbue.remote_service_connector.errors import R2BucketNotFoundError
from imbue.remote_service_connector.http_api import handle_endpoint_errors
from imbue.remote_service_connector.r2.buckets import best_effort_revoke_token
from imbue.remote_service_connector.r2.naming import DESTROYED_WORKSPACE_BACKUP_RETENTION_SECONDS
from imbue.remote_service_connector.r2.naming import R2_BUCKET_NAME_SEP
from imbue.remote_service_connector.r2.naming import RESERVED_BUCKET_SHORT_NAME_PREFIXES
from imbue.remote_service_connector.r2.naming import WORKSPACE_BACKUP_SHORT_NAME_RE
from imbue.remote_service_connector.r2.naming import bucket_owner_prefix
from imbue.remote_service_connector.r2.naming import parse_workspace_backup_bucket_name
from imbue.remote_service_connector.r2.stores import KeyStore
from imbue.remote_service_connector.r2.sweep import run_r2_quota_sweep
from imbue.remote_service_connector.sync import OrphanBucketStore
from imbue.remote_service_connector.sync import SyncStore

logger = logging.getLogger(__name__)

router = APIRouter()


# Per-pass work budgets: how many destroyed-record candidates to process and
# how many R2 objects to delete in total (across all buckets) in one pass.
_REAP_RECORD_BUDGET_PER_PASS: Final[int] = 25
_REAP_OBJECT_BUDGET_PER_PASS: Final[int] = 2000
_REAP_LIST_PAGE_SIZE: Final[int] = 500


def _revoke_bucket_keys_any_owner(ops: CloudflareOps, key_store: KeyStore, bucket_name: str) -> int:
    """Delete + best-effort-revoke every recorded key for a bucket, regardless of owner."""
    revoked_count = 0
    for row in key_store.list_all_keys():
        if row.get("bucket_name") != bucket_name:
            continue
        key_store.delete_key(str(row["access_key_id"]))
        best_effort_revoke_token(ops, str(row["access_key_id"]))
        revoked_count += 1
    return revoked_count


def _reap_bucket_bounded(
    ops: CloudflareOps,
    key_store: KeyStore,
    bucket_name: str,
    object_budget: int,
    # ("deleted" | "partial" | "missing", objects_deleted)
) -> tuple[str, int]:
    """Empty a bucket within ``object_budget`` deletions and delete it when empty.

    Returns "partial" when the budget ran out before the bucket was empty (the
    next pass continues), "missing" when the bucket does not exist.
    """
    objects_deleted = 0
    try:
        keys = ops.list_bucket_object_keys(bucket_name, _REAP_LIST_PAGE_SIZE)
    except R2BucketNotFoundError:
        return "missing", objects_deleted
    while keys:
        for key in keys:
            if objects_deleted >= object_budget:
                return "partial", objects_deleted
            ops.delete_bucket_object(bucket_name, key)
            objects_deleted += 1
        try:
            keys = ops.list_bucket_object_keys(bucket_name, _REAP_LIST_PAGE_SIZE)
        except R2BucketNotFoundError:
            return "missing", objects_deleted
    try:
        ops.delete_bucket(bucket_name)
    except R2BucketNotFoundError:
        return "missing", objects_deleted
    _revoke_bucket_keys_any_owner(ops, key_store, bucket_name)
    return "deleted", objects_deleted


def _record_backup_bucket_name(record: dict[str, Any]) -> str | None:
    """The bucket a destroyed record's backups live in, or None when it has none.

    Prefers the record's explicit ``backup_bucket`` (verified to be a
    reserved-shape bucket in the record owner's own namespace -- the value is
    client-supplied, so an unverifiable name gets no bucket work rather than a
    delete of an arbitrary bucket). Falls back to the legacy name derivation
    from the host id for records that predate the explicit column.
    """
    user_id = str(record["user_id"])
    owner_prefix = bucket_owner_prefix(derive_user_id_prefix(user_id))
    explicit = record.get("backup_bucket")
    if explicit:
        explicit_name = str(explicit)
        if explicit_name.startswith(owner_prefix) and parse_workspace_backup_bucket_name(explicit_name) is not None:
            return explicit_name
        logger.warning("Ignoring unverifiable backup_bucket %r on a destroyed record", explicit_name)
        return None
    host_id = str(record["host_id"])
    if WORKSPACE_BACKUP_SHORT_NAME_RE.match(host_id):
        return f"{owner_prefix}{host_id}"
    return None


def _is_bucket_referenced_by_another_record(sync_store: SyncStore, bucket_name: str, workspace_id: str) -> bool:
    """Whether any record other than ``workspace_id``'s still references the bucket."""
    parsed = parse_workspace_backup_bucket_name(bucket_name)
    if parsed is None:
        # _record_backup_bucket_name only yields parseable names; treat an
        # unparseable one as referenced (never delete on uncertain evidence).
        return True
    user_id_prefix, short_name = parsed
    return sync_store.any_record_references_backup_bucket(
        user_id_prefix, bucket_name, short_name, excluding_workspace_id=workspace_id
    )


def run_backup_retention_reap(
    ops: CloudflareOps,
    sync_store: SyncStore,
    key_store: KeyStore,
    orphan_store: OrphanBucketStore,
    window_seconds: float = DESTROYED_WORKSPACE_BACKUP_RETENTION_SECONDS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """One reap pass: destroyed records past the window, then orphan buckets past their stamp.

    ``dry_run`` reports the candidate list without deleting anything (and
    without writing orphan stamps).
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=window_seconds)
    counters = {
        "records_reaped": 0,
        "orphan_buckets_reaped": 0,
        "buckets_deleted": 0,
        "buckets_kept_referenced": 0,
        "buckets_partially_emptied": 0,
        "objects_deleted": 0,
    }
    candidates: list[dict[str, Any]] = []
    object_budget_left = _REAP_OBJECT_BUDGET_PER_PASS

    # Phase 1: destroyed records past the window. Bucket first (strict order);
    # the record is deleted only once its bucket is gone, so a failed or
    # partial bucket delete leaves the record for the next pass.
    for record in sync_store.list_destroyed_records_before(cutoff)[:_REAP_RECORD_BUDGET_PER_PASS]:
        user_id = str(record["user_id"])
        host_id = str(record["host_id"])
        workspace_id = str(record["agent_id"])
        bucket_name = _record_backup_bucket_name(record)
        if dry_run:
            candidates.append(
                {
                    "kind": "record",
                    "workspace_id": workspace_id,
                    "host_id": host_id,
                    "bucket_name": bucket_name,
                    "destroyed_at": str(record["destroyed_at"]),
                    "age_seconds": (now - record["destroyed_at"]).total_seconds(),
                }
            )
            continue
        if object_budget_left <= 0:
            break
        if bucket_name is not None and _is_bucket_referenced_by_another_record(sync_store, bucket_name, workspace_id):
            # Grandfathered host-named buckets can be shared (machine reuse),
            # and a record's explicit backup_bucket is client-supplied: while
            # any OTHER record of the account still references the bucket, it
            # may hold live backups, so only the record is reaped. The bucket
            # falls to the last referencing record's own reap (or the orphan
            # sweep) once nothing references it.
            logger.info(
                "Keeping backup bucket %s of reaped workspace %s: another record still references it",
                bucket_name,
                workspace_id,
            )
            counters["buckets_kept_referenced"] += 1
            sync_store.delete_record_by_workspace(user_id, workspace_id)
            counters["records_reaped"] += 1
            continue
        if bucket_name is not None:
            outcome, objects_deleted = _reap_bucket_bounded(ops, key_store, bucket_name, object_budget_left)
            object_budget_left -= objects_deleted
            counters["objects_deleted"] += objects_deleted
            if outcome == "partial":
                counters["buckets_partially_emptied"] += 1
                continue
            if outcome == "deleted":
                counters["buckets_deleted"] += 1
            orphan_store.delete_stamp(bucket_name)
        sync_store.delete_record_by_workspace(user_id, workspace_id)
        counters["records_reaped"] += 1

    # Phase 2: workspace-backup buckets no record references. First sighting
    # stamps the orphan clock; reap once the stamp ages past the window. Both
    # naming generations are swept: workspace-id-named buckets and the
    # grandfathered host-id-named ones.
    orphan_candidates = [
        bucket
        for short_prefix in RESERVED_BUCKET_SHORT_NAME_PREFIXES
        for bucket in ops.list_buckets(name_contains=f"{R2_BUCKET_NAME_SEP}{short_prefix}")
    ]
    for bucket in orphan_candidates:
        bucket_name = str(bucket.get("name", ""))
        parsed = parse_workspace_backup_bucket_name(bucket_name)
        if parsed is None:
            continue
        user_id_prefix, short_name = parsed
        if sync_store.any_record_references_backup_bucket(user_id_prefix, bucket_name, short_name):
            if not dry_run:
                orphan_store.delete_stamp(bucket_name)
            continue
        if dry_run:
            first_seen = orphan_store.get_first_seen(bucket_name)
            candidates.append(
                {
                    "kind": "orphan",
                    "bucket_name": bucket_name,
                    "first_seen_orphaned_at": str(first_seen) if first_seen is not None else None,
                    "age_seconds": (now - first_seen).total_seconds() if first_seen is not None else None,
                }
            )
            continue
        first_seen = orphan_store.get_or_record_first_seen(bucket_name)
        if first_seen >= cutoff:
            continue
        if object_budget_left <= 0:
            break
        outcome, objects_deleted = _reap_bucket_bounded(ops, key_store, bucket_name, object_budget_left)
        object_budget_left -= objects_deleted
        counters["objects_deleted"] += objects_deleted
        if outcome == "partial":
            counters["buckets_partially_emptied"] += 1
            continue
        if outcome == "deleted":
            counters["buckets_deleted"] += 1
        orphan_store.delete_stamp(bucket_name)
        counters["orphan_buckets_reaped"] += 1

    if dry_run:
        return {"dry_run": True, "window_seconds": window_seconds, "candidates": candidates}
    return counters


@router.get("/policies/destroyed-workspace-backups")
def destroyed_workspace_backup_policy_endpoint() -> dict[str, float]:
    """The backup retention policy for destroyed workspaces (public; the value is not sensitive)."""
    return {"retention_seconds": DESTROYED_WORKSPACE_BACKUP_RETENTION_SECONDS}


@router.post("/admin/sweep/backup-retention")
def admin_run_backup_retention_reap(
    request: Request, dry_run: bool = False, window_seconds: float | None = None
) -> dict[str, object]:
    """Run one backup-retention reap pass on demand (operator tool + deployment tests).

    Authenticated by the fixed operator admin key (``MINDS_ADMIN_KEY``).
    ``dry_run=1`` returns the candidate list without deleting anything;
    ``window_seconds`` overrides the retention window (admin-only, e.g. 0 to
    reap fresh tombstones in a deployment test).
    """
    with handle_endpoint_errors():
        require_admin_key(request)
        result = run_backup_retention_reap(
            cloudflare_module.get_cloudflare_ctx().ops,
            sync_module.get_sync_store(),
            r2_stores_module.get_key_store(),
            sync_module.get_orphan_bucket_store(),
            window_seconds=(
                window_seconds if window_seconds is not None else DESTROYED_WORKSPACE_BACKUP_RETENTION_SECONDS
            ),
            dry_run=dry_run,
        )
        return {"status": "completed", "result": result}


@router.post("/admin/sweep/r2")
def admin_run_r2_sweep(request: Request, email: str | None = None) -> dict[str, object]:
    """Run one R2 storage-quota sweep pass on demand (operator tool + deployment tests).

    Authenticated by the fixed operator admin key (``MINDS_ADMIN_KEY``),
    NOT the SuperTokens auth path. An optional ``email`` query parameter
    scopes the pass to one account (resolved via SuperTokens); without it the
    pass covers every account, exactly like the hourly cron.
    """
    with handle_endpoint_errors():
        require_admin_key(request)
        only_user_id = accounts_module.resolve_user_id_by_email(email) if email else None
        counters = run_r2_quota_sweep(
            cloudflare_module.get_cloudflare_ctx().ops,
            r2_stores_module.get_key_store(),
            entitlements_module.get_entitlements_store(),
            r2_stores_module.get_grant_store(),
            only_user_id=only_user_id,
        )
        return {"status": "completed", "counters": counters}
