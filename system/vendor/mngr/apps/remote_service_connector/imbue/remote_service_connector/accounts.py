"""Account endpoints: plan + usage, paid-list CRUD, and email-addressed admin management.

Same fixed-key auth as the paid-list CRUD (``MINDS_ADMIN_KEY``); the
operator addresses users by email and the connector resolves the SuperTokens
user. ``show`` lazily creates the entitlements row (so a subsequent
``set-quota`` always has a row to update); ``set-plan`` always resets to the
plan's defaults (the operator's way to wipe manual bumps) and deliberately
skips the ally eligibility check -- the operator knows best.
"""

import concurrent.futures
import logging
from collections.abc import Mapping
from typing import Final

import httpx
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field
from supertokens_python.syncio import list_users_by_account_info
from supertokens_python.types.base import AccountInfoInput

import imbue.remote_service_connector.accounts_web as accounts_web_module
import imbue.remote_service_connector.auth as auth_module
import imbue.remote_service_connector.cloudflare as cloudflare_module
import imbue.remote_service_connector.entitlements as entitlements_module
import imbue.remote_service_connector.litellm_client as litellm_client
import imbue.remote_service_connector.sync as sync_module
from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector import db
from imbue.remote_service_connector.auth import authenticate_request
from imbue.remote_service_connector.auth import clear_paid_status_cache
from imbue.remote_service_connector.auth import derive_user_id_prefix
from imbue.remote_service_connector.auth import require_admin_key
from imbue.remote_service_connector.auth import require_ally_eligible
from imbue.remote_service_connector.auth import require_verified_email
from imbue.remote_service_connector.auth_proxy import AUTH_TENANT_ID
from imbue.remote_service_connector.cloudflare import CloudflareOps
from imbue.remote_service_connector.entitlements import AccountEntitlements
from imbue.remote_service_connector.entitlements import EntitlementsStore
from imbue.remote_service_connector.entitlements import INTEGER_ENTITLEMENT_NAMES
from imbue.remote_service_connector.entitlements import PLAN_ALLY
from imbue.remote_service_connector.entitlements import PlanEntitlements
from imbue.remote_service_connector.entitlements import QUOTA_ENTITLEMENT_NAMES
from imbue.remote_service_connector.errors import CloudflareApiError
from imbue.remote_service_connector.errors import InvalidPaidListEntryError
from imbue.remote_service_connector.hosts import count_leased_hosts
from imbue.remote_service_connector.hosts import count_total_workspaces
from imbue.remote_service_connector.http_api import handle_endpoint_errors
from imbue.remote_service_connector.r2.buckets import list_owned_buckets
from imbue.remote_service_connector.r2.buckets import read_bucket_usage_bytes_concurrently
from imbue.remote_service_connector.sync import WorkspaceRecordState

logger = logging.getLogger(__name__)

router = APIRouter()


class AccountUsage(BaseModel):
    """Live usage numbers for the account, one per quota entitlement."""

    remote_workspaces: int = Field(description="Current running workspaces (leased/stopping/starting rows)")
    total_workspaces: int = Field(default=0, description="Current running + stopped workspaces")
    buckets: int = Field(description="Current R2 buckets")
    total_bucket_bytes: int = Field(description="Total bytes across the account's buckets (live REST usage)")
    llm_spend_usd_this_period: float = Field(description="LiteLLM aggregate spend in the current budget period")
    llm_budget_resets_at: str | None = Field(
        default=None, description="When the rolling LLM budget period resets (from LiteLLM), if known"
    )
    active_synced_workspaces: int = Field(description="Current ACTIVE synced workspace records")


class AccountInfoResponse(BaseModel):
    """The caller's plan, entitlement values, and live usage."""

    user_id: str = Field(description="SuperTokens user id")
    email: str = Field(
        description="The caller's email -- a verified login-method email when one exists, otherwise the"
        " unverified primary email (empty when the account has none)"
    )
    plan_name: str = Field(description="Current plan name")
    entitlements: PlanEntitlements = Field(description="The account's current entitlement values")
    usage: AccountUsage = Field(description="Live usage, computed at request time")
    available_plans: list[str] = Field(
        default_factory=list, description="Every plan name currently seeded (for plan-selector UIs)"
    )


class SetPlanRequest(BaseModel):
    plan: str = Field(description="Plan name to switch to (e.g. 'explorer' or 'ally')")


class AdminSetPlanRequest(BaseModel):
    plan: str = Field(description="Plan name to assign (resets the user's entitlements to the plan's defaults)")


class AdminSetQuotaRequest(BaseModel):
    entitlement: str = Field(description="Quota entitlement name (one of QUOTA_ENTITLEMENT_NAMES)")
    value: float = Field(description="New value (must be a whole number for count/byte entitlements)")


class PaidListEntryRequest(BaseModel):
    value: str = Field(description="The domain or email to add/remove (normalized to lowercase server-side)")


class PaidDomainInfo(BaseModel):
    domain: str = Field(description="The allowed domain (lowercased)")
    is_paid: bool = Field(description="Whether this domain currently grants paid access")
    created_at: str = Field(description="When the row was first inserted")
    updated_at: str = Field(description="When is_paid was last changed")


class PaidEmailInfo(BaseModel):
    email: str = Field(description="The allowed email (lowercased)")
    is_paid: bool = Field(description="Whether this email currently grants paid access")
    created_at: str = Field(description="When the row was first inserted")
    updated_at: str = Field(description="When is_paid was last changed")


def _normalize_paid_domain(value: str) -> str:
    """Lowercase + validate a domain entry (no ``@``, no internal whitespace, non-empty)."""
    normalized = value.strip().lower()
    if not normalized:
        raise InvalidPaidListEntryError(value, "domain must not be empty")
    if "@" in normalized:
        raise InvalidPaidListEntryError(value, "domain must not contain '@' (use the email list for full addresses)")
    if any(character.isspace() for character in normalized):
        raise InvalidPaidListEntryError(value, "domain must not contain whitespace")
    return normalized


def _normalize_paid_email(value: str) -> str:
    """Lowercase + validate an email entry (exactly one ``@`` with non-empty local + domain parts)."""
    normalized = value.strip().lower()
    local, separator, domain = normalized.partition("@")
    if not separator or not local or not domain or "@" in domain or any(c.isspace() for c in normalized):
        raise InvalidPaidListEntryError(value, "email must be of the form 'local@domain'")
    return normalized


def _list_paid_entries(table: str, value_column: str, paid_only: bool) -> list[tuple[str, bool, str, str]]:
    """Return all rows of a paid-list table as ``(value, is_paid, created_at, updated_at)`` tuples."""
    where_clause = " WHERE is_paid = TRUE" if paid_only else ""
    conn = db.get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {value_column}, is_paid, created_at, updated_at FROM {table}{where_clause} "
                f"ORDER BY {value_column} ASC"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [(row[0], bool(row[1]), str(row[2]), str(row[3])) for row in rows]


def _activate_paid_entry(table: str, value_column: str, value: str) -> None:
    """Upsert a paid-list entry to ``is_paid = true`` (reactivating in place, keeping created_at)."""
    conn = db.get_pool_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {table} ({value_column}, is_paid, created_at, updated_at) "
                    "VALUES (%s, TRUE, NOW(), NOW()) "
                    f"ON CONFLICT ({value_column}) DO UPDATE SET is_paid = TRUE, updated_at = NOW()",
                    (value,),
                )
    finally:
        conn.close()
    clear_paid_status_cache()


def _deactivate_paid_entry(table: str, value_column: str, value: str) -> None:
    """Soft-delete a paid-list entry (``is_paid = false``). A no-op when the row is absent."""
    conn = db.get_pool_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {table} SET is_paid = FALSE, updated_at = NOW() WHERE {value_column} = %s",
                    (value,),
                )
    finally:
        conn.close()
    clear_paid_status_cache()


@router.get("/paid/domains")
def list_paid_domains(request: Request, paid_only: bool = False) -> list[dict[str, object]]:
    """List paid-domain rows. ``paid_only=true`` filters to currently-active entries."""
    with handle_endpoint_errors():
        require_admin_key(request)
        rows = _list_paid_entries("paid_domains", "domain", paid_only)
        return [
            PaidDomainInfo(domain=value, is_paid=is_paid, created_at=created_at, updated_at=updated_at).model_dump()
            for (value, is_paid, created_at, updated_at) in rows
        ]


@router.post("/paid/domains/add")
def add_paid_domain(request: Request, body: PaidListEntryRequest) -> dict[str, object]:
    """Add (or reactivate) a paid domain. Idempotent."""
    with handle_endpoint_errors():
        require_admin_key(request)
        domain = _normalize_paid_domain(body.value)
        _activate_paid_entry("paid_domains", "domain", domain)
        return {"status": "added", "domain": domain}


@router.post("/paid/domains/remove")
def remove_paid_domain(request: Request, body: PaidListEntryRequest) -> dict[str, object]:
    """Soft-remove a paid domain (set is_paid=false). Idempotent."""
    with handle_endpoint_errors():
        require_admin_key(request)
        domain = _normalize_paid_domain(body.value)
        _deactivate_paid_entry("paid_domains", "domain", domain)
        return {"status": "removed", "domain": domain}


@router.get("/paid/emails")
def list_paid_emails(request: Request, paid_only: bool = False) -> list[dict[str, object]]:
    """List paid-email rows. ``paid_only=true`` filters to currently-active entries."""
    with handle_endpoint_errors():
        require_admin_key(request)
        rows = _list_paid_entries("paid_emails", "email", paid_only)
        return [
            PaidEmailInfo(email=value, is_paid=is_paid, created_at=created_at, updated_at=updated_at).model_dump()
            for (value, is_paid, created_at, updated_at) in rows
        ]


@router.post("/paid/emails/add")
def add_paid_email(request: Request, body: PaidListEntryRequest) -> dict[str, object]:
    """Add (or reactivate) a paid email. Idempotent."""
    with handle_endpoint_errors():
        require_admin_key(request)
        email = _normalize_paid_email(body.value)
        _activate_paid_entry("paid_emails", "email", email)
        # Deliberately no auto-verification: paid-listing an email must never
        # mark it verified (verification is proof of mailbox ownership, and
        # ally eligibility requires the real thing). Verification is
        # non-blocking everywhere else, so the account is not locked out.
        return {"status": "added", "email": email}


@router.post("/paid/emails/remove")
def remove_paid_email(request: Request, body: PaidListEntryRequest) -> dict[str, object]:
    """Soft-remove a paid email (set is_paid=false). Idempotent."""
    with handle_endpoint_errors():
        require_admin_key(request)
        email = _normalize_paid_email(body.value)
        _deactivate_paid_entry("paid_emails", "email", email)
        return {"status": "removed", "email": email}


def _count_active_sync_records(user_id: str) -> int:
    records = sync_module.get_sync_store().list_records(user_id)
    return sum(1 for r in records if r["state"] == WorkspaceRecordState.ACTIVE.value)


def summarize_owner_bucket_usage(ops: CloudflareOps, user_id_prefix: str) -> tuple[int, int]:
    """Return the owner's (bucket_count, total_bytes) from live REST usage reads.

    Display-only semantics: a failed read for one bucket logs a warning and
    counts that bucket as zero rather than failing the whole request.
    """
    bucket_names = [str(bucket.get("name", "")) for bucket in list_owned_buckets(ops, user_id_prefix)]
    total_bucket_bytes = 0
    for bucket_name, result in zip(bucket_names, read_bucket_usage_bytes_concurrently(ops, bucket_names), strict=True):
        if isinstance(result, (CloudflareApiError, httpx.HTTPError)):
            # Display-only degradation on a transient upstream read: counted,
            # not error-reported.
            emit_metric("cloudflare_api_failed", 1, {"operation": "display_bucket_usage_read"})
            logger.info("Failed to read usage for bucket %s: %s", bucket_name, result)
        else:
            total_bucket_bytes += result
    return len(bucket_names), total_bucket_bytes


def compute_account_usage(ops: CloudflareOps, user_id_prefix: str, user_id: str) -> AccountUsage:
    """Compute the account's live usage numbers, querying the upstream sources concurrently.

    The network-backed sources (per-bucket REST usage, LiteLLM spend) are
    independent and run concurrently; the two DB-backed counts stay on the
    request thread because the stores' psycopg2 connections are not
    shared-safe across threads. Bucket byte counts come from the real-time
    per-bucket REST usage endpoint (bounded by the account's bucket quota,
    itself read concurrently).
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        bucket_summary_future = pool.submit(summarize_owner_bucket_usage, ops, user_id_prefix)
        llm_spend_future = pool.submit(litellm_client.get_litellm_user_spend, user_id)
        leased_host_count = count_leased_hosts(user_id_prefix)
        total_workspace_count = count_total_workspaces(user_id_prefix)
        active_sync_count = _count_active_sync_records(user_id)
        bucket_count, total_bucket_bytes = bucket_summary_future.result()
        spend, reset_at = llm_spend_future.result()
    return AccountUsage(
        remote_workspaces=leased_host_count,
        total_workspaces=total_workspace_count,
        buckets=bucket_count,
        total_bucket_bytes=total_bucket_bytes,
        llm_spend_usd_this_period=spend,
        llm_budget_resets_at=reset_at,
        active_synced_workspaces=active_sync_count,
    )


# CLEANUP: remove these compatibility fields (and both helpers) once the
# access log's imbue_client field shows no in-window strict-model client
# (any minds release predating the tolerant WireModel parsing) -- the support
# window is ~1 month after the first tolerant release ships. v0.3.11 desktop
# clients parse the /account and plan responses into models that REQUIRE the
# Cloudflare-tunnel-era fields (max_tunnels / max_services_per_tunnel in
# entitlements, tunnels in usage) with no defaults, so the first deploy after
# migration 020 dropped those columns would break their Accounts page with a
# validation error. Served as hardcoded zeros ("no tunnels exist anymore"),
# which old clients render as an inert usage row.
_DEPRECATED_TUNNEL_ENTITLEMENT_FIELDS: Final[dict[str, int]] = {"max_tunnels": 0, "max_services_per_tunnel": 0}


def _with_deprecated_tunnel_entitlement_fields(entitlements_payload: Mapping[str, object]) -> dict[str, object]:
    """Add the tunnel-era entitlement fields v0.3.11 clients require (see CLEANUP above)."""
    return {**entitlements_payload, **_DEPRECATED_TUNNEL_ENTITLEMENT_FIELDS}


def _with_deprecated_tunnel_account_fields(account_payload: dict[str, object]) -> dict[str, object]:
    """Return a copy with the tunnel-era entitlement + usage fields v0.3.11 clients require (see CLEANUP above)."""
    result = dict(account_payload)
    entitlements_payload = result.get("entitlements")
    usage_payload = result.get("usage")
    if isinstance(entitlements_payload, dict):
        result["entitlements"] = {**entitlements_payload, **_DEPRECATED_TUNNEL_ENTITLEMENT_FIELDS}
    if isinstance(usage_payload, dict):
        result["usage"] = {**usage_payload, "tunnels": 0}
    return result


@router.get("/account")
def get_account(request: Request) -> dict[str, object]:
    """Return the caller's plan, entitlement values, and live usage.

    Lazily creates the entitlements row on first touch (like every other
    quota-relevant endpoint), so this is also the cheapest way for a client
    to materialize an account's plan.
    """
    with handle_endpoint_errors():
        ops = cloudflare_module.get_cloudflare_ctx().ops
        user = authenticate_request(request)
        token = request.headers.get("authorization", "")[7:]
        user_id = auth_module.get_user_id_from_access_token(token)
        # The backfill's paid-list check may only consume a verified email --
        # an unverified account gets a plain free row.
        entitlements = entitlements_module.ensure_account_entitlements(
            user_id=user_id, user_id_prefix=user.user_id_prefix, email=user.verified_email or ""
        )
        usage = compute_account_usage(ops, user.user_id_prefix, user_id)
        return _with_deprecated_tunnel_account_fields(
            AccountInfoResponse(
                user_id=user_id,
                email=user.email or "",
                plan_name=entitlements.plan_name,
                entitlements=entitlements.quota_values(),
                usage=usage,
                available_plans=[
                    str(p["plan_name"]) for p in entitlements_module.get_entitlements_store().list_plans()
                ],
            ).model_dump()
        )


def apply_plan_to_account(user_id: str, plan_name: str, store: "EntitlementsStore | None" = None) -> PlanEntitlements:
    """Reset an account's entitlements wholesale to a plan's defaults.

    Pushes the plan's monthly LLM budget to LiteLLM *first*: a failed push
    fails the whole operation, so the DB row and LiteLLM never diverge.
    """
    entitlements_store = store if store is not None else entitlements_module.get_entitlements_store()
    plan = entitlements_store.get_plan(plan_name)
    if plan is None:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {plan_name!r}")
    litellm_client.upsert_litellm_user_budget(user_id, float(plan["monthly_llm_spend_usd"]))
    entitlements_store.update_entitlements(
        user_id, {"plan_name": plan_name, **{name: plan[name] for name in QUOTA_ENTITLEMENT_NAMES}}
    )
    return PlanEntitlements(**{name: plan[name] for name in QUOTA_ENTITLEMENT_NAMES})


@router.post("/account/plan")
def set_account_plan(request: Request, body: SetPlanRequest) -> dict[str, object]:
    """Switch the caller's plan, resetting their entitlements to the plan's defaults.

    Re-selecting the current plan is a no-op (so idempotent client retries
    never wipe operator-granted bumps). Switching to 'ally' requires a
    *verified*, paid-listed email -- eligibility is domain ownership, so an
    unverified email may never satisfy it.
    """
    with handle_endpoint_errors():
        # Resolved via the shared web-identity helper so the POST verifies the
        # session against the core (revoked sessions are refused immediately)
        # and the hosted chrome's cookie sessions work here too.
        user, user_id = accounts_web_module.resolve_web_user_identity(request)
        entitlements = entitlements_module.resolve_entitlements_for_user(user_id, user)
        if body.plan == entitlements.plan_name:
            return {
                "plan_name": entitlements.plan_name,
                "entitlements": _with_deprecated_tunnel_entitlement_fields(entitlements.quota_values().model_dump()),
            }
        if body.plan == PLAN_ALLY:
            require_verified_email(user)
            require_ally_eligible(user.verified_email)
        new_values = apply_plan_to_account(entitlements.user_id, body.plan)
        return {
            "plan_name": body.plan,
            "entitlements": _with_deprecated_tunnel_entitlement_fields(new_values.model_dump()),
        }


def resolve_user_id_by_email(email: str) -> str:
    """Resolve a SuperTokens user id from an email; 404 when no account exists."""
    users = list_users_by_account_info(
        tenant_id=AUTH_TENANT_ID,
        account_info=AccountInfoInput(email=email.strip().lower()),
    )
    if not users:
        raise HTTPException(status_code=404, detail=f"No account found for email {email!r}")
    return str(users[0].id)


def _admin_ensure_entitlements(email: str) -> AccountEntitlements:
    user_id = resolve_user_id_by_email(email)
    user_id_prefix = derive_user_id_prefix(user_id)
    return entitlements_module.ensure_account_entitlements(user_id=user_id, user_id_prefix=user_id_prefix, email=email)


@router.get("/admin/accounts/{email}")
def admin_get_account(request: Request, email: str) -> dict[str, object]:
    """Operator view of one account: plan, entitlements, and live usage."""
    with handle_endpoint_errors():
        require_admin_key(request)
        entitlements = _admin_ensure_entitlements(email)
        usage = compute_account_usage(
            cloudflare_module.get_cloudflare_ctx().ops, entitlements.user_id_prefix, entitlements.user_id
        )
        payload = _with_deprecated_tunnel_account_fields(
            AccountInfoResponse(
                user_id=entitlements.user_id,
                email=email.strip().lower(),
                plan_name=entitlements.plan_name,
                entitlements=entitlements.quota_values(),
                usage=usage,
                available_plans=[
                    str(p["plan_name"]) for p in entitlements_module.get_entitlements_store().list_plans()
                ],
            ).model_dump()
        )
        # Suspension state is operator-facing only, so it rides the admin
        # response rather than the shared AccountInfoResponse model.
        payload["suspended_at"] = entitlements.suspended_at
        payload["suspended_reason"] = entitlements.suspended_reason
        return payload


@router.post("/admin/accounts/{email}/plan")
def admin_set_account_plan(request: Request, email: str, body: AdminSetPlanRequest) -> dict[str, object]:
    """Assign a plan to an account, resetting its entitlements to the plan's defaults."""
    with handle_endpoint_errors():
        require_admin_key(request)
        entitlements = _admin_ensure_entitlements(email)
        new_values = apply_plan_to_account(entitlements.user_id, body.plan)
        return {
            "plan_name": body.plan,
            "entitlements": _with_deprecated_tunnel_entitlement_fields(new_values.model_dump()),
        }


@router.post("/admin/accounts/{email}/quota")
def admin_set_account_quota(request: Request, email: str, body: AdminSetQuotaRequest) -> dict[str, object]:
    """Set a single entitlement value on an account (an operator bump)."""
    with handle_endpoint_errors():
        require_admin_key(request)
        if body.entitlement not in QUOTA_ENTITLEMENT_NAMES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown entitlement {body.entitlement!r}; must be one of {list(QUOTA_ENTITLEMENT_NAMES)}",
            )
        if body.entitlement in INTEGER_ENTITLEMENT_NAMES and body.value != int(body.value):
            raise HTTPException(
                status_code=400, detail=f"Entitlement {body.entitlement!r} requires a whole number, got {body.value}"
            )
        if body.value < 0:
            raise HTTPException(status_code=400, detail="Entitlement values must be non-negative")
        entitlements = _admin_ensure_entitlements(email)
        value: float | int = body.value if body.entitlement == "monthly_llm_spend_usd" else int(body.value)
        if body.entitlement == "monthly_llm_spend_usd":
            litellm_client.upsert_litellm_user_budget(entitlements.user_id, float(value))
        entitlements_module.get_entitlements_store().update_entitlements(
            entitlements.user_id, {body.entitlement: value}
        )
        return {"status": "updated", "entitlement": body.entitlement, "value": value}
