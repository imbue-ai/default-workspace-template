"""R2 storage-cleanup grant + recheck endpoints."""

import logging
from typing import Final

from fastapi import APIRouter
from fastapi import Request

import imbue.remote_service_connector.accounts_web as accounts_web_module
import imbue.remote_service_connector.cloudflare as cloudflare_module
import imbue.remote_service_connector.entitlements as entitlements_module
import imbue.remote_service_connector.r2.stores as stores_module
import imbue.remote_service_connector.r2.sweep as sweep_module
from imbue.remote_service_connector.errors import CleanupGrantBudgetExhaustedError
from imbue.remote_service_connector.http_api import handle_endpoint_errors
from imbue.remote_service_connector.r2.buckets import CleanupGrantResponse
from imbue.remote_service_connector.r2.buckets import StorageRecheckResponse
from imbue.remote_service_connector.r2.buckets import key_info_from_row
from imbue.remote_service_connector.r2.buckets import measure_live_owner_usage_bytes
from imbue.remote_service_connector.r2.stores import R2_CLEANUP_GRANT_EXPIRY_MINUTES
from imbue.remote_service_connector.r2.stores import R2_CLEANUP_GRANT_FAILED_BUDGET
from imbue.remote_service_connector.r2.stores import R2_CLEANUP_GRANT_WINDOW_HOURS
from imbue.remote_service_connector.r2.stores import r2_enforcement_lease

logger = logging.getLogger(__name__)

router = APIRouter()

# How long the grant/recheck endpoints wait for the owner's enforcement
# lease. Contention here means the hourly sweep (or a suspension) is
# mid-enforcement for this account -- seconds of work -- so a bounded wait
# usually succeeds; past it the caller gets a retryable 503.
_ENDPOINT_LEASE_WAIT_SECONDS: Final = 30.0


@router.post("/account/storage-cleanup-grant")
def create_storage_cleanup_grant(request: Request) -> dict[str, object]:
    """Temporarily restore the caller's sweep-downgraded bucket keys for client-side cleanup.

    restic cleanup (forget + prune) needs full write access -- prune repacks
    data, so no permission level allows delete-but-not-put. The grant flips
    the downgraded keys back to readwrite; it settles at the caller's
    /account/storage-recheck (or at expiry via the sweep), and only grants
    that settle without ANY usage decrease burn the rolling failed-grant
    budget. Idempotent: an active grant is returned as-is (flipping any keys
    still downgraded), and an account with nothing downgraded gets a
    'not_needed' no-op.
    """
    with handle_endpoint_errors():
        ops = cloudflare_module.get_cloudflare_ctx().ops
        user, full_user_id = accounts_web_module.resolve_web_user_identity(request)
        entitlements = entitlements_module.resolve_entitlements_for_user(full_user_id, user)
        key_store = stores_module.get_key_store()
        grant_store = stores_module.get_grant_store()
        counters = {"keys_downgraded": 0, "keys_restored": 0, "key_update_failures": 0}
        with r2_enforcement_lease(entitlements.user_id, wait_timeout_seconds=_ENDPOINT_LEASE_WAIT_SECONDS) as lease:
            rows = key_store.list_keys(entitlements.user_id, None)
            active_grant = grant_store.get_active_grant(entitlements.user_id)
            # A 'pending' marker counts as downgraded: the token's live policy
            # is untrusted, and the restore pass below settles it.
            is_any_key_downgraded = any(row.get("enforced_access") is not None for row in rows)
            if active_grant is None and not is_any_key_downgraded:
                return CleanupGrantResponse(
                    status="not_needed", keys=[key_info_from_row(row) for row in rows]
                ).model_dump()
            if active_grant is None:
                failed_count = grant_store.count_failed_grants_in_window(
                    entitlements.user_id, R2_CLEANUP_GRANT_WINDOW_HOURS
                )
                if failed_count >= R2_CLEANUP_GRANT_FAILED_BUDGET:
                    raise CleanupGrantBudgetExhaustedError(
                        limit=R2_CLEANUP_GRANT_FAILED_BUDGET,
                        current=failed_count,
                        window_hours=R2_CLEANUP_GRANT_WINDOW_HOURS,
                    )
                baseline_bytes = measure_live_owner_usage_bytes(ops, user.user_id_prefix)
                active_grant = grant_store.create_grant(
                    entitlements.user_id, user.user_id_prefix, baseline_bytes, R2_CLEANUP_GRANT_EXPIRY_MINUTES
                )
            # Restore every still-downgraded key (is_over_quota=False path).
            sweep_module.enforce_owner_key_access(ops, key_store, rows, False, counters, lease)
            refreshed_rows = key_store.list_keys(entitlements.user_id, None)
        return CleanupGrantResponse(
            status="granted",
            expires_at=str(active_grant["expires_at"]),
            baseline_bytes=int(active_grant["baseline_bytes"]),
            keys=[key_info_from_row(row) for row in refreshed_rows],
        ).model_dump()


@router.post("/account/storage-recheck")
def recheck_storage_enforcement(request: Request) -> dict[str, object]:
    """Re-measure the caller's live storage usage and apply enforcement immediately.

    Works standalone (a user who freed space any other way gets their keys
    restored without waiting for the hourly sweep) and doubles as the
    settlement point for an outstanding cleanup grant: settled usage below
    the grant's baseline -- any decrease -- marks the grant successful.
    Reads the same real-time REST usage the sweep's downgrade confirmation
    uses, so this endpoint and the sweep can never disagree about the same
    measurement.
    """
    with handle_endpoint_errors():
        ops = cloudflare_module.get_cloudflare_ctx().ops
        user, full_user_id = accounts_web_module.resolve_web_user_identity(request)
        entitlements = entitlements_module.resolve_entitlements_for_user(full_user_id, user)
        key_store = stores_module.get_key_store()
        grant_store = stores_module.get_grant_store()
        counters = {"keys_downgraded": 0, "keys_restored": 0, "key_update_failures": 0}
        with r2_enforcement_lease(entitlements.user_id, wait_timeout_seconds=_ENDPOINT_LEASE_WAIT_SECONDS) as lease:
            live_bytes = measure_live_owner_usage_bytes(ops, user.user_id_prefix)
            is_over_quota = live_bytes > entitlements.max_total_bucket_bytes
            unsettled_grants = grant_store.list_unsettled_grants(entitlements.user_id)
            for grant in unsettled_grants:
                grant_store.settle_grant(int(grant["grant_id"]), live_bytes, live_bytes < int(grant["baseline_bytes"]))
            rows = key_store.list_keys(entitlements.user_id, None)
            sweep_module.enforce_owner_key_access(ops, key_store, rows, is_over_quota, counters, lease)
            refreshed_rows = key_store.list_keys(entitlements.user_id, None)
        return StorageRecheckResponse(
            usage_bytes=live_bytes,
            limit_bytes=entitlements.max_total_bucket_bytes,
            is_over_quota=is_over_quota,
            is_grant_settled=bool(unsettled_grants),
            keys=[key_info_from_row(row) for row in refreshed_rows],
        ).model_dump()
