"""R2 storage-quota sweep (hourly cron body).

Hourly cron: reads every bucket's peak stored bytes from the GraphQL
analytics dataset (one query per sweep regardless of bucket count, one row
per bucket), sums per owner, and flips bucket-key token policies in place --
readwrite keys of an over-quota owner become read-only (same S3
credentials, so reads keep working while writes fail), and are restored
automatically once the owner is back under quota. The GraphQL number is a
lookback-window *peak*, so it is only a screening filter: before any
downgrade the owner is re-measured with the real-time REST usage endpoint
(the same source the grant/recheck endpoints read), which makes the sweep
and an out-of-band restore unable to disagree. The sweep also settles
expired cleanup grants, skips owners with an active grant (so a mid-prune
measurement never re-locks them), and permanently enforces the
single-key-per-bucket invariant: any bucket with more than one recorded key
has the extras revoked (newest wins), which doubles as the one-time cleanup
of multi-key buckets minted before this model.
"""

import logging
from collections.abc import Callable
from typing import Any
from typing import Final

import httpx
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError

import imbue.remote_service_connector.auth as auth_module
import imbue.remote_service_connector.entitlements as entitlements_module
from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector.cloudflare import CloudflareOps
from imbue.remote_service_connector.entitlements import EntitlementsStore
from imbue.remote_service_connector.errors import CloudflareApiError
from imbue.remote_service_connector.errors import R2EnforcementLeaseLostError
from imbue.remote_service_connector.errors import R2EnforcementLeaseUnavailableError
from imbue.remote_service_connector.r2.buckets import measure_live_owner_usage_bytes
from imbue.remote_service_connector.r2.naming import R2_BUCKET_NAME_SEP
from imbue.remote_service_connector.r2.naming import r2_token_name
from imbue.remote_service_connector.r2.stores import EnforcementLease
from imbue.remote_service_connector.r2.stores import GrantStore
from imbue.remote_service_connector.r2.stores import KeyStore
from imbue.remote_service_connector.r2.stores import LeaseStore
from imbue.remote_service_connector.r2.stores import R2_ENFORCEMENT_PENDING
from imbue.remote_service_connector.r2.stores import r2_enforcement_lease

logger = logging.getLogger(__name__)

# How long the sweep waits for one owner's enforcement lease before skipping
# that owner. Contention means a grant/recheck/suspension is mid-flight for
# the owner; the sweep simply retries next hour, so a short wait suffices.
_SWEEP_LEASE_WAIT_SECONDS: Final = 5.0


def _sweep_owner_email(user_id: str, email_getter: Callable[[str], str | None]) -> str | None:
    """Best-effort backfill-email lookup for the sweep's lazy row creation.

    The default getter follows :func:`imbue.remote_service_connector.auth.get_backfill_email`
    semantics: a verified email, ``""`` for an existing-but-unverified owner
    (create the row, skip the paid check), or ``None`` for an owner whose
    SuperTokens record cannot be resolved (the sweep must skip them).
    """
    try:
        return email_getter(user_id)
    except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
        emit_metric("supertokens_user_fetch_failed", 1, {"caller": "r2_sweep"})
        logger.warning("Sweep could not resolve email for user %s", user_id[:8], exc_info=exc)
        return None


def _revoke_extra_bucket_keys(
    ops: CloudflareOps,
    key_store: KeyStore,
    counters: dict[str, int],
    # When set, only this owner's keys are considered (the email-scoped admin sweep).
    only_user_id: str | None,
) -> dict[str, list[dict[str, Any]]]:
    """Enforce the single-key-per-bucket invariant; returns the surviving keys grouped by owner.

    The newest key per (owner, bucket) survives; extras are revoked and their
    rows dropped, counted in ``counters["extra_keys_revoked"]``. The row is
    dropped only after a successful Cloudflare revoke: the ``r2_keys`` table
    is the sole record of keys, so dropping the row of a still-live token
    would orphan a credential no later sweep could revoke or downgrade. A
    failed revoke is logged, counted in ``counters["key_update_failures"]``,
    and retried on the next sweep.
    """
    keys_by_owner: dict[str, list[dict[str, Any]]] = {}
    keys_by_owner_bucket: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in key_store.list_all_keys():
        if only_user_id is not None and str(row["owner_user_id"]) != only_user_id:
            continue
        keys_by_owner_bucket.setdefault((str(row["owner_user_id"]), str(row["bucket_name"])), []).append(row)
    for (owner_user_id, _bucket_name), rows in keys_by_owner_bucket.items():
        ordered = sorted(rows, key=lambda r: str(r["created_at"]))
        for extra in ordered[:-1]:
            access_key_id = str(extra["access_key_id"])
            try:
                ops.delete_bucket_token(access_key_id)
            except (CloudflareApiError, httpx.HTTPError) as exc:
                emit_metric("cloudflare_api_failed", 1, {"operation": "sweep_revoke_extra_key"})
                logger.warning("Sweep failed to revoke extra key %s", access_key_id, exc_info=exc)
                counters["key_update_failures"] += 1
                continue
            key_store.delete_key(access_key_id)
            counters["extra_keys_revoked"] += 1
        keys_by_owner.setdefault(owner_user_id, []).append(ordered[-1])
    return keys_by_owner


def _resolve_owner_storage_limit_bytes(
    owner_user_id: str,
    owner_prefix: str,
    entitlements_store: EntitlementsStore,
    email_getter: Callable[[str], str | None],
    existing_row: dict[str, Any] | None,
) -> int | None:
    """Resolve the owner's storage limit, lazily creating their entitlements row when needed.

    ``existing_row`` is the owner's already-fetched entitlements row (None when
    the owner has none yet). Mirrors the request-path rule (paid pre-cutoff
    accounts land on ally; an existing-but-unverified owner gets a plain
    free row). Returns ``None`` only for an owner whose SuperTokens record
    cannot be resolved at all -- the sweep must skip them, never enforce
    against guessed limits.
    """
    if existing_row is not None:
        return int(existing_row["max_total_bucket_bytes"])
    email = _sweep_owner_email(owner_user_id, email_getter)
    if email is None:
        return None
    entitlements = entitlements_module.ensure_account_entitlements(
        user_id=owner_user_id, user_id_prefix=owner_prefix, email=email, store=entitlements_store
    )
    return entitlements.max_total_bucket_bytes


def enforce_owner_key_access(
    ops: CloudflareOps,
    key_store: KeyStore,
    rows: list[dict[str, Any]],
    is_over_quota: bool,
    counters: dict[str, int],
    lease: EnforcementLease,
) -> None:
    """Downgrade (or restore) one owner's bucket-key token policies around the storage quota.

    Must run under the owner's enforcement lease; the lease is renewed (and
    ownership proven) before each key's Cloudflare call, so a taken-over
    pass aborts at a key boundary (raising R2EnforcementLeaseLostError)
    instead of interleaving with the new holder. A ``'pending'``
    ``enforced_access`` marker is written before every transition's
    Cloudflare call (downgrades and restores alike), so a crash
    mid-transition leaves the key recorded as untrusted (and re-asserted on
    the next pass) rather than confidently recorded in a state the live
    token policy may no longer match.

    A failed Cloudflare token update is logged and counted, skipping only
    that key.
    """
    for row in rows:
        # A key under suspension enforcement is owned by the suspend/unsuspend
        # flow; the sweep must not downgrade or restore it (the owner loop
        # already skips suspended accounts -- this guards the race where the
        # account's state changed mid-pass).
        if row.get("suspension_access") is not None:
            continue
        # The desired end state for this key under the current quota verdict.
        # A 'pending' marker never equals the desired marker, so an in-flight
        # transition is always re-asserted rather than trusted.
        is_downgrade_wanted = is_over_quota and str(row["access"]) == "readwrite"
        desired_policy = "read" if is_downgrade_wanted else str(row["access"])
        desired_marker = "read" if is_downgrade_wanted else None
        if row.get("enforced_access") == desired_marker:
            continue
        access_key_id = str(row["access_key_id"])
        bucket_name = str(row["bucket_name"])
        token_name = r2_token_name(bucket_name, row.get("alias"))
        lease.renew_or_raise()
        try:
            if row.get("enforced_access") != R2_ENFORCEMENT_PENDING:
                # Write-ahead marker: recorded before the Cloudflare call so
                # the policy state is never silently unknown. Restores need it
                # too -- a restore that crashed after its Cloudflare write
                # would otherwise keep its settled 'read' marker, which a
                # later over-quota pass trusts as already-downgraded.
                key_store.set_enforced_access(access_key_id, R2_ENFORCEMENT_PENDING)
            ops.update_bucket_token_access(access_key_id, bucket_name, desired_policy, token_name)
            key_store.set_enforced_access(access_key_id, desired_marker)
            counters["keys_downgraded" if desired_marker is not None else "keys_restored"] += 1
        except (CloudflareApiError, httpx.HTTPError) as exc:
            emit_metric("cloudflare_api_failed", 1, {"operation": "sweep_update_token"})
            logger.warning("Sweep failed to update token %s for bucket %s", access_key_id, bucket_name, exc_info=exc)
            counters["key_update_failures"] += 1


def _settle_expired_grants(
    ops: CloudflareOps,
    grant_store: GrantStore,
    counters: dict[str, int],
    only_user_id: str | None,
) -> None:
    """Settle cleanup grants whose expiry passed without an explicit recheck.

    Settlement measures live usage via the REST endpoint (the same source the
    grant's baseline came from); a failed read skips only that grant, which
    stays unsettled and is retried next pass.
    """
    for grant in grant_store.list_expired_unsettled_grants():
        if only_user_id is not None and str(grant["user_id"]) != only_user_id:
            continue
        try:
            live_bytes = measure_live_owner_usage_bytes(ops, str(grant["user_id_prefix"]))
        except (CloudflareApiError, httpx.HTTPError) as exc:
            emit_metric("cloudflare_api_failed", 1, {"operation": "sweep_settle_grant_usage_read"})
            logger.warning("Sweep failed to settle grant %s (usage read failed)", grant["grant_id"], exc_info=exc)
            counters["grant_settle_failures"] += 1
            continue
        grant_store.settle_grant(int(grant["grant_id"]), live_bytes, live_bytes < int(grant["baseline_bytes"]))
        counters["grants_settled"] += 1


def run_r2_quota_sweep(
    ops: CloudflareOps,
    key_store: KeyStore,
    entitlements_store: EntitlementsStore,
    grant_store: GrantStore,
    email_getter: Callable[[str], str | None] = auth_module.get_backfill_email,
    # None resolves the Neon-backed store; tests inject an in-memory one.
    lease_store: LeaseStore | None = None,
    lease_wait_seconds: float = _SWEEP_LEASE_WAIT_SECONDS,
    only_user_id: str | None = None,
) -> dict[str, int]:
    """Run one storage-quota sweep pass; returns counters for the cron log.

    Fails loudly (raises) when the account-wide usage query fails or fills
    its row budget -- a sweep that cannot see usage must not look like a
    clean pass. Per-user failures (email lookup, a Cloudflare token update)
    are logged and skip only that user/key, and an unknown limit skips the
    user entirely. Missing data never *downgrades* a key: a bucket absent
    from the analytics window counts as zero usage, and a downgrade is only
    applied after the real-time REST usage confirms the account is over its
    limit (the GraphQL peak alone can only restore or screen).
    """
    counters = {
        "extra_keys_revoked": 0,
        "users_over_quota": 0,
        "keys_downgraded": 0,
        "keys_restored": 0,
        "users_skipped": 0,
        "users_skipped_for_grant": 0,
        "users_skipped_suspended": 0,
        "key_update_failures": 0,
        "grants_settled": 0,
        "grant_settle_failures": 0,
        "downgrades_cancelled_by_live_usage": 0,
        "live_usage_read_failures": 0,
        "users_skipped_lease_contended": 0,
        "users_aborted_lease_lost": 0,
    }

    # Enforce the single-key-per-bucket invariant first: newest key per
    # (owner, bucket) survives, extras are revoked + dropped.
    keys_by_owner = _revoke_extra_bucket_keys(ops, key_store, counters, only_user_id)

    # Settle grants whose expiry passed without a recheck (the fallback path;
    # a live client normally settles via /account/storage-recheck).
    _settle_expired_grants(ops, grant_store, counters, only_user_id)

    # One GraphQL query covers every bucket's peak stored bytes; a failure
    # (or a possibly-truncated full page) aborts the sweep by raising rather
    # than being mistaken for zero usage.
    usage_by_bucket = ops.query_r2_storage_by_bucket()
    all_buckets = [str(b.get("name", "")) for b in ops.list_buckets()]

    for owner_user_id, rows in keys_by_owner.items():
        # An active grant means client-side cleanup may be mid-prune (which
        # transiently *increases* usage); leave the owner alone until the
        # grant settles.
        if grant_store.get_active_grant(owner_user_id) is not None:
            counters["users_skipped_for_grant"] += 1
            continue

        # A suspended owner's keys are held in whatever state the suspend
        # action put them (read-only or disabled); leave them alone entirely
        # -- the unsuspend fan-out re-applies the correct quota state.
        owner_entitlements_row = entitlements_store.get_entitlements(owner_user_id)
        if owner_entitlements_row is not None and owner_entitlements_row.get("suspended_at"):
            counters["users_skipped_suspended"] += 1
            continue

        owner_prefix = str(rows[0]["bucket_name"]).split(R2_BUCKET_NAME_SEP, 1)[0]
        bucket_prefix = f"{owner_prefix}{R2_BUCKET_NAME_SEP}"
        owner_buckets = [name for name in all_buckets if name.startswith(bucket_prefix)]
        owner_peak_bytes = sum(usage_by_bucket.get(name, 0) for name in owner_buckets)

        limit_bytes = _resolve_owner_storage_limit_bytes(
            owner_user_id, owner_prefix, entitlements_store, email_getter, owner_entitlements_row
        )
        if limit_bytes is None:
            logger.warning(
                "Sweep skipping user %s: no resolvable verified email for lazy plan assignment", owner_user_id[:8]
            )
            counters["users_skipped"] += 1
            continue

        # The peak over the lookback window screens candidates; peak under
        # the limit proves live usage is under (restores need no confirm).
        # Over-peak owners are re-measured with the real-time REST endpoint
        # so a user who just cleaned up is never re-downgraded on stale data.
        is_over_quota = owner_peak_bytes > limit_bytes
        if is_over_quota:
            try:
                live_bytes = measure_live_owner_usage_bytes(ops, owner_prefix)
            except (CloudflareApiError, httpx.HTTPError) as exc:
                emit_metric("cloudflare_api_failed", 1, {"operation": "sweep_live_usage_read"})
                logger.warning("Sweep skipping user %s: live usage read failed", owner_user_id[:8], exc_info=exc)
                counters["live_usage_read_failures"] += 1
                counters["users_skipped"] += 1
                continue
            if live_bytes <= limit_bytes:
                counters["downgrades_cancelled_by_live_usage"] += 1
            is_over_quota = live_bytes > limit_bytes

        if is_over_quota:
            counters["users_over_quota"] += 1
        try:
            with r2_enforcement_lease(
                owner_user_id, wait_timeout_seconds=lease_wait_seconds, store=lease_store
            ) as lease:
                # Re-check under the lease before downgrading: a cleanup grant
                # may have been created (restoring the keys under this same
                # lease) between the loop-top check and lease acquisition, and
                # a downgrade here would break the mid-cleanup guarantee.
                # Restores need no re-check -- restoring is exactly what a
                # grant wants.
                if is_over_quota and grant_store.get_active_grant(owner_user_id) is not None:
                    counters["users_skipped_for_grant"] += 1
                    continue
                # Re-read the keys under the lease: the pass-start rows may
                # predate a concurrent grant/suspension transition, and the
                # enforcement decisions key off the recorded markers.
                locked_rows = key_store.list_keys(owner_user_id, None)
                enforce_owner_key_access(ops, key_store, locked_rows, is_over_quota, counters, lease)
        except R2EnforcementLeaseUnavailableError:
            # A grant/recheck/suspension is mid-flight for this owner; the
            # next hourly pass retries.
            counters["users_skipped_lease_contended"] += 1
        except R2EnforcementLeaseLostError:
            counters["users_aborted_lease_lost"] += 1
    return counters
