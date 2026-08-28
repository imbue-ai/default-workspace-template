"""Operator account-suspension endpoints: suspend, unsuspend, revoke-sessions.

Suspend is a multi-system fan-out (flag + sessions + workspaces + LiteLLM
keys + R2 keys + shares), so it is built to be idempotent and re-runnable:
the flag is set first (locking the account out of every session-creation
path, so a partial run still leaves the front door closed), then each step
runs independently and reports its outcome. The response's per-step report is
how the operator sees a partial failure; re-running the same command
converges. Unsuspend mirrors this with the flag cleared first, so a partial
restore never leaves the user locked out of sign-in.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final

import httpx
import psycopg2
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError
from supertokens_python.recipe.session.syncio import revoke_all_sessions_for_user

import imbue.remote_service_connector.accounts as accounts_module
import imbue.remote_service_connector.cloudflare as cloudflare_module
import imbue.remote_service_connector.entitlements as entitlements_module
import imbue.remote_service_connector.litellm_client as litellm_client
import imbue.remote_service_connector.r2.stores as stores_module
import imbue.remote_service_connector.shares as shares_module
import imbue.remote_service_connector.workspaces as workspaces_module
from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector.auth import derive_user_id_prefix
from imbue.remote_service_connector.auth import require_admin_key
from imbue.remote_service_connector.auth_proxy import require_supertokens_configured
from imbue.remote_service_connector.cloudflare import CloudflareOps
from imbue.remote_service_connector.errors import CloudflareApiError
from imbue.remote_service_connector.errors import MissingStorageConfigError
from imbue.remote_service_connector.errors import R2EnforcementLeaseLostError
from imbue.remote_service_connector.errors import R2EnforcementLeaseUnavailableError
from imbue.remote_service_connector.http_api import handle_endpoint_errors
from imbue.remote_service_connector.r2.naming import r2_token_name
from imbue.remote_service_connector.r2.stores import KeyStore
from imbue.remote_service_connector.r2.stores import R2_ENFORCEMENT_PENDING
from imbue.remote_service_connector.r2.stores import R2_SUSPENSION_PENDING_DISABLED
from imbue.remote_service_connector.r2.stores import R2_SUSPENSION_PENDING_READ
from imbue.remote_service_connector.r2.stores import r2_enforcement_lease

logger = logging.getLogger(__name__)

router = APIRouter()

# Exception types a fan-out step may fail with without failing the whole
# suspend/unsuspend request: the step is recorded as errored in the report
# and the operator re-runs. HTTPException covers litellm_request's admin-API
# failures; the lease errors cover a contended/taken-over enforcement lease;
# anything outside this tuple is a programming error and surfaces as the
# usual 500.
_STEP_ERROR_TYPES = (
    HTTPException,
    httpx.HTTPError,
    CloudflareApiError,
    MissingStorageConfigError,
    R2EnforcementLeaseLostError,
    R2EnforcementLeaseUnavailableError,
    SuperTokensSessionError,
    SuperTokensGeneralError,
    psycopg2.Error,
)

# How long the suspend/unsuspend storage steps wait for the account's
# enforcement lease. An operator is watching the request, so waiting out a
# whole in-flight grant/recheck/sweep pass is preferable to a partial report.
_SUSPENSION_LEASE_WAIT_SECONDS: Final = 60.0


class SuspendAccountRequest(BaseModel):
    reason: str = Field(description="Operator-recorded reason (internal; required, never shown to the user)")
    block_storage: bool = Field(
        default=False,
        description=(
            "Disable the account's R2 tokens outright (reads included) instead of the default "
            "read-only downgrade. Re-running suspend with this flag escalates; re-running without "
            "it never de-escalates."
        ),
    )


def _run_step(step_name: str, step_fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run one fan-out step, converting expected failures into a report entry."""
    try:
        result = step_fn()
    except _STEP_ERROR_TYPES as exc:
        emit_metric("suspension_step_failed", 1, {"step": step_name})
        logger.warning("Suspension step %s failed", step_name, exc_info=exc)
        return {"status": "error", "error": str(exc)}
    return {"status": "ok", **result}


def _revoke_sessions_step(user_id: str) -> dict[str, Any]:
    revoked = revoke_all_sessions_for_user(user_id=user_id)
    return {"revoked_count": len(revoked)}


def _list_llm_key_tokens(user_id: str) -> list[str]:
    keys_raw = litellm_client.list_litellm_user_key_entries(user_id)
    return [str(entry.get("token")) for entry in keys_raw if isinstance(entry, dict) and entry.get("token")]


def _block_llm_keys_step(user_id: str, is_blocking: bool) -> dict[str, Any]:
    """Block (or unblock) every LiteLLM virtual key of the account.

    Key-level because LiteLLM has no internal-user-level block; new keys
    cannot appear while suspended (minting requires a session).
    """
    path = "/key/block" if is_blocking else "/key/unblock"
    tokens = _list_llm_key_tokens(user_id)
    for token in tokens:
        litellm_client.litellm_request("POST", path, json_body={"key": token})
    return {"key_count": len(tokens)}


def _effective_key_access(row: dict[str, Any]) -> str:
    """The access scope the key's Cloudflare policies currently grant.

    Conservative about in-flight markers: a quota-'pending', suspension
    'pending_read', or suspension 'pending_disabled' key is treated as
    read-only (the unconfirmed transition targeted read, or overwrote an
    earlier read downgrade's marker), so the scope reported here never
    overstates what the token grants.
    """
    if row.get("suspension_access") in (
        "read",
        R2_SUSPENSION_PENDING_READ,
        R2_SUSPENSION_PENDING_DISABLED,
    ) or row.get("enforced_access") in (
        "read",
        R2_ENFORCEMENT_PENDING,
    ):
        return "read"
    return str(row["access"])


def _desired_suspension_transition(row: dict[str, Any], is_storage_blocked: bool) -> tuple[str, str] | None:
    """The (pending_marker, settled_marker) pair to drive for one key, or None to leave it alone.

    Blocked runs disable everything not already disabled (escalation, and the
    retry of any in-flight marker). Non-blocked runs downgrade untouched keys
    to read-only unless their read-only state is *confirmed* (natively read,
    or a settled quota downgrade -- a quota-'pending' key's live policy is
    unconfirmed and may still be readwrite, and the sweep that would settle
    it skips suspended accounts, so suspension must re-drive it), and finish
    in-flight transitions in their original direction -- a 'pending_disabled'
    left by an earlier blocked run is re-driven to 'disabled', never
    de-escalated.
    """
    suspension_marker = row.get("suspension_access")
    if is_storage_blocked:
        if suspension_marker == "disabled":
            return None
        return (R2_SUSPENSION_PENDING_DISABLED, "disabled")
    if suspension_marker in ("read", "disabled"):
        return None
    if suspension_marker == R2_SUSPENSION_PENDING_DISABLED:
        return (R2_SUSPENSION_PENDING_DISABLED, "disabled")
    if suspension_marker == R2_SUSPENSION_PENDING_READ:
        return (R2_SUSPENSION_PENDING_READ, "read")
    is_confirmed_read_only = str(row["access"]) == "read" or row.get("enforced_access") == "read"
    if not is_confirmed_read_only:
        return (R2_SUSPENSION_PENDING_READ, "read")
    return None


def _suspend_storage_keys_step(
    ops: CloudflareOps, key_store: KeyStore, user_id: str, is_storage_blocked: bool
) -> dict[str, Any]:
    """Apply the suspension's storage enforcement to every R2 key of the account.

    Default: flip effectively-readwrite keys to read-only in place (backups
    stay retrievable). ``is_storage_blocked``: disable the tokens outright.
    Each key's transition writes its directional pending marker BEFORE the
    Cloudflare call and settles it after, so a crash mid-transition leaves
    the key recorded as in-flight (re-driven on the next run) instead of
    untouched; a re-run retries exactly the keys that failed or were left
    pending. The marker also tells the quota sweep and the unsuspend restore
    what to undo. Per-key failures are counted and reported, not fatal.
    """
    downgraded_count = 0
    disabled_count = 0
    failed_count = 0
    with r2_enforcement_lease(user_id, wait_timeout_seconds=_SUSPENSION_LEASE_WAIT_SECONDS) as lease:
        for row in key_store.list_keys(user_id):
            transition = _desired_suspension_transition(row, is_storage_blocked)
            if transition is None:
                continue
            pending_marker, settled_marker = transition
            access_key_id = str(row["access_key_id"])
            bucket_name = str(row["bucket_name"])
            token_name = r2_token_name(bucket_name, row.get("alias"))
            lease.renew_or_raise()
            try:
                if row.get("suspension_access") != pending_marker:
                    key_store.set_suspension_access(access_key_id, pending_marker)
                if settled_marker == "disabled":
                    ops.set_bucket_token_status(
                        access_key_id, bucket_name, _effective_key_access(row), token_name, "disabled"
                    )
                else:
                    ops.update_bucket_token_access(access_key_id, bucket_name, "read", token_name)
                key_store.set_suspension_access(access_key_id, settled_marker)
                if settled_marker == "disabled":
                    disabled_count += 1
                else:
                    downgraded_count += 1
            except (CloudflareApiError, httpx.HTTPError) as exc:
                emit_metric("cloudflare_api_failed", 1, {"operation": "suspend_key"})
                logger.warning("Suspension failed to update R2 key %s", access_key_id, exc_info=exc)
                failed_count += 1
    result: dict[str, Any] = {
        "downgraded_count": downgraded_count,
        "disabled_count": disabled_count,
        "failed_count": failed_count,
    }
    if failed_count:
        result["status"] = "error"
        result["error"] = f"{failed_count} R2 key update(s) failed; re-run suspend to retry"
    return result


def _restore_storage_keys_step(ops: CloudflareOps, key_store: KeyStore, user_id: str) -> dict[str, Any]:
    """Undo the suspension's storage enforcement, restoring quota-appropriate access.

    A disabled token is re-activated and its policies re-asserted in the same
    PUT; a read-only downgrade is restored to whatever the quota sweep's
    ``enforced_access`` (or the key's own scope) dictates -- so an account
    that went over its storage quota while suspended comes back read-only,
    not readwrite.
    """
    restored_count = 0
    failed_count = 0
    with r2_enforcement_lease(user_id, wait_timeout_seconds=_SUSPENSION_LEASE_WAIT_SECONDS) as lease:
        for row in key_store.list_keys(user_id):
            if row.get("suspension_access") is None:
                continue
            access_key_id = str(row["access_key_id"])
            bucket_name = str(row["bucket_name"])
            token_name = r2_token_name(bucket_name, row.get("alias"))
            # A quota marker ('read', or the unconfirmed 'pending') keeps the
            # restored policy read-only; the next sweep/recheck settles it.
            desired_access = "read" if row.get("enforced_access") is not None else str(row["access"])
            lease.renew_or_raise()
            try:
                # An in-flight 'pending_disabled' may or may not have landed;
                # the status flip to active also re-asserts the policies, so
                # it reconciles both outcomes. Restores need no write-ahead
                # marker of their own: the suspension marker is cleared only
                # after the Cloudflare call succeeds, so a crashed restore is
                # simply retried.
                if row["suspension_access"] in ("disabled", R2_SUSPENSION_PENDING_DISABLED):
                    ops.set_bucket_token_status(access_key_id, bucket_name, desired_access, token_name, "active")
                else:
                    ops.update_bucket_token_access(access_key_id, bucket_name, desired_access, token_name)
                key_store.set_suspension_access(access_key_id, None)
                restored_count += 1
            except (CloudflareApiError, httpx.HTTPError) as exc:
                emit_metric("cloudflare_api_failed", 1, {"operation": "unsuspend_key"})
                logger.warning("Unsuspension failed to restore R2 key %s", access_key_id, exc_info=exc)
                failed_count += 1
    result: dict[str, Any] = {"restored_count": restored_count, "failed_count": failed_count}
    if failed_count:
        result["status"] = "error"
        result["error"] = f"{failed_count} R2 key restore(s) failed; re-run unsuspend to retry"
    return result


def _resolve_account(email: str) -> tuple[str, str]:
    """Resolve ``(user_id, user_id_prefix)`` for an operator-addressed email."""
    user_id = accounts_module.resolve_user_id_by_email(email)
    return user_id, derive_user_id_prefix(user_id)


@router.post("/admin/accounts/{email}/revoke-sessions")
def admin_revoke_account_sessions(request: Request, email: str) -> dict[str, object]:
    """Revoke every SuperTokens session of the addressed account (the D2 operator action).

    Standalone (no suspension flag): the next state-modifying request with a
    now-revoked token is refused within one round-trip (those routes verify
    sessions against the core), while read access drains out over the access
    token's remaining lifetime. The account can sign in again -- suspend is
    the action that also blocks that.
    """
    with handle_endpoint_errors():
        require_admin_key(request)
        require_supertokens_configured()
        user_id, _user_id_prefix = _resolve_account(email)
        revoked = revoke_all_sessions_for_user(user_id=user_id)
        emit_metric("admin_sessions_revoked", 1, {})
        logger.info("Operator revoked %d sessions for account %s", len(revoked), user_id[:8])
        return {"status": "OK", "email": email.strip().lower(), "user_id": user_id, "revoked_count": len(revoked)}


@router.post("/admin/accounts/{email}/suspend")
def admin_suspend_account(request: Request, email: str, body: SuspendAccountRequest) -> dict[str, object]:
    """Suspend the addressed account: flag + sessions + workspaces + keys + shares.

    Everything is reversible and data-preserving (see ``unsuspend``); the
    per-step report shows exactly what happened, and re-running converges
    after a partial failure (with ``block_storage`` it also escalates an
    existing suspension's storage enforcement).
    """
    with handle_endpoint_errors():
        require_admin_key(request)
        require_supertokens_configured()
        reason = body.reason.strip()
        if not reason:
            raise HTTPException(status_code=400, detail="A non-empty suspension reason is required")
        user_id, user_id_prefix = _resolve_account(email)

        # The flag first: with it set, every session-creation path refuses the
        # account, so even a run that fails partway leaves the front door
        # closed and a re-run converges. The row is materialized like every
        # other admin path; a re-run refreshes the reason but keeps the
        # original suspension timestamp.
        entitlements_module.ensure_account_entitlements(
            user_id=user_id, user_id_prefix=user_id_prefix, email=email.strip().lower()
        )
        store = entitlements_module.get_entitlements_store()
        existing = store.get_entitlements(user_id)
        already_suspended_at = existing.get("suspended_at") if existing is not None else None
        if already_suspended_at:
            store.update_entitlements(user_id, {"suspended_reason": reason})
        else:
            # An ISO string rather than a datetime: Postgres casts it to the
            # timestamptz column, and reads come back as strings either way.
            store.update_entitlements(
                user_id,
                {"suspended_at": datetime.now(timezone.utc).isoformat(), "suspended_reason": reason},
            )
        emit_metric("account_suspended", 1, {"block_storage": str(body.block_storage).lower()})
        logger.info("Suspending account %s (block_storage=%s)", user_id[:8], body.block_storage)

        ops = cloudflare_module.get_cloudflare_ctx().ops
        key_store = stores_module.get_key_store()
        user_label = shares_module.derive_share_user_label(user_id)
        steps = {
            "sessions": _run_step("sessions", lambda: _revoke_sessions_step(user_id)),
            "workspaces": _run_step(
                "workspaces", lambda: workspaces_module.begin_stopping_all_leased_workspaces(user_id_prefix)
            ),
            "llm_keys": _run_step("llm_keys", lambda: _block_llm_keys_step(user_id, is_blocking=True)),
            "storage_keys": _run_step(
                "storage_keys",
                lambda: _suspend_storage_keys_step(ops, key_store, user_id, body.block_storage),
            ),
            "shares": _run_step(
                "shares",
                lambda: {"suspended_count": shares_module.get_share_store().suspend_shares_for_user(user_label)},
            ),
        }
        updated = store.get_entitlements(user_id)
        return {
            "status": "ok" if all(step["status"] == "ok" for step in steps.values()) else "partial",
            "email": email.strip().lower(),
            "user_id": user_id,
            "suspended_at": updated.get("suspended_at") if updated is not None else None,
            "steps": steps,
        }


@router.post("/admin/accounts/{email}/unsuspend")
def admin_unsuspend_account(request: Request, email: str) -> dict[str, object]:
    """Lift the account's suspension: clear the flag and restore what suspend changed.

    The flag is cleared first so a partial restore never leaves the user
    locked out of sign-in; re-running retries any failed restore step.
    Workspaces stay stopped (the user starts them), and no sessions are
    restored (the user signs in fresh). Suspended shares return to active,
    and the workspace's retained relay token makes the tunnel come back on
    its own once the workspace runs again.
    """
    with handle_endpoint_errors():
        require_admin_key(request)
        require_supertokens_configured()
        user_id, _user_id_prefix = _resolve_account(email)

        store = entitlements_module.get_entitlements_store()
        existing = store.get_entitlements(user_id)
        if existing is not None and existing.get("suspended_at"):
            store.update_entitlements(user_id, {"suspended_at": None, "suspended_reason": None})
        emit_metric("account_unsuspended", 1, {})
        logger.info("Unsuspending account %s", user_id[:8])

        ops = cloudflare_module.get_cloudflare_ctx().ops
        key_store = stores_module.get_key_store()
        user_label = shares_module.derive_share_user_label(user_id)
        steps = {
            "llm_keys": _run_step("llm_keys", lambda: _block_llm_keys_step(user_id, is_blocking=False)),
            "storage_keys": _run_step("storage_keys", lambda: _restore_storage_keys_step(ops, key_store, user_id)),
            "shares": _run_step(
                "shares",
                lambda: {"reactivated_count": shares_module.get_share_store().unsuspend_shares_for_user(user_label)},
            ),
        }
        return {
            "status": "ok" if all(step["status"] == "ok" for step in steps.values()) else "partial",
            "email": email.strip().lower(),
            "user_id": user_id,
            "steps": steps,
        }
