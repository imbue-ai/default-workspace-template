"""Plans and per-account entitlements (quotas).

Plans (git-owned, written from deploy.toml on every deploy) define the
default entitlements a user receives when a plan is assigned. Each account
gets its own ``account_entitlements`` row -- created lazily on first
quota-relevant touch -- whose values are copied wholesale from the plan and
are the operator-adjustable source of truth thereafter. Changing a plan's
defaults never retroactively changes existing rows.
"""

import functools
import logging
from collections.abc import Callable
from typing import Any
from typing import NoReturn
from typing import Protocol

from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError
from supertokens_python.syncio import get_user

import imbue.remote_service_connector.auth as auth_module
from imbue.remote_service_connector import db
from imbue.remote_service_connector.auth import UserAuth
from imbue.remote_service_connector.auth import is_email_paid
from imbue.remote_service_connector.errors import PlanNotFoundError
from imbue.remote_service_connector.errors import QuotaExceededError
from imbue.remote_service_connector.errors import UnknownEntitlementColumnError

logger = logging.getLogger(__name__)


# The quota entitlements every plan (and every per-user row) carries. This
# tuple is the single authority for which columns exist; the admin set-quota
# endpoint validates entitlement names against it.
QUOTA_ENTITLEMENT_NAMES: tuple[str, ...] = (
    "max_remote_workspaces",
    "max_tunnels",
    "max_services_per_tunnel",
    "max_buckets",
    "max_total_bucket_bytes",
    "monthly_llm_spend_usd",
    "max_active_synced_workspaces",
)

# Entitlement columns holding integer counts/bytes (everything except the
# monthly LLM spend, which is a USD amount).
INTEGER_ENTITLEMENT_NAMES: frozenset[str] = frozenset(QUOTA_ENTITLEMENT_NAMES) - {"monthly_llm_spend_usd"}


class PlanEntitlements(BaseModel):
    """The quota values a plan grants (also the per-user entitlement values)."""

    max_remote_workspaces: int = Field(description="Max concurrent pool-host leases (running or stopped)")
    max_tunnels: int = Field(description="Max Cloudflare tunnels")
    max_services_per_tunnel: int = Field(description="Max forwarded services per tunnel")
    max_buckets: int = Field(description="Max R2 buckets")
    max_total_bucket_bytes: int = Field(description="Max total bytes across all the account's buckets")
    monthly_llm_spend_usd: float = Field(description="Monthly LLM spend cap in USD (rolling; 0 disables key minting)")
    max_active_synced_workspaces: int = Field(description="Max ACTIVE synced workspace records")


PLAN_EXPLORER = "explorer"
PLAN_ALLY = "ally"

# Ship-time cutoff for the lazy-backfill rule: accounts whose SuperTokens
# ``time_joined`` predates this instant get the paid-list-based initial plan
# (ally when paid-listed); accounts created after it always start as explorer
# and must select ally explicitly. 2026-07-21T00:00:00Z, in the epoch
# milliseconds SuperTokens uses for ``time_joined``.
_PREEXISTING_ACCOUNT_CUTOFF_EPOCH_MS = 1784592000000

_QUOTA_COLUMNS_SQL = ", ".join(QUOTA_ENTITLEMENT_NAMES)
_PLAN_COLUMNS_SQL = f"plan_name, {_QUOTA_COLUMNS_SQL}"
_ENTITLEMENT_COLUMNS_SQL = f"user_id, user_id_prefix, plan_name, {_QUOTA_COLUMNS_SQL}"


class AccountEntitlements(PlanEntitlements):
    """One account's entitlement row: identity fields plus the quota values."""

    user_id: str = Field(description="Full SuperTokens user id (row key)")
    user_id_prefix: str = Field(description="16-hex user-id prefix used to namespace tunnels/leases/buckets")
    plan_name: str = Field(description="The plan this row was last assigned from")

    def quota_values(self) -> PlanEntitlements:
        return PlanEntitlements(
            max_remote_workspaces=self.max_remote_workspaces,
            max_tunnels=self.max_tunnels,
            max_services_per_tunnel=self.max_services_per_tunnel,
            max_buckets=self.max_buckets,
            max_total_bucket_bytes=self.max_total_bucket_bytes,
            monthly_llm_spend_usd=self.monthly_llm_spend_usd,
            max_active_synced_workspaces=self.max_active_synced_workspaces,
        )


def _quota_values_from_row(row: tuple[Any, ...], offset: int) -> dict[str, Any]:
    """Map the trailing quota columns of a SELECT row into name->value pairs."""
    values: dict[str, Any] = {}
    for idx, name in enumerate(QUOTA_ENTITLEMENT_NAMES):
        raw = row[offset + idx]
        values[name] = float(raw) if name == "monthly_llm_spend_usd" else int(raw)
    return values


class EntitlementsStore(Protocol):
    """Abstraction over the plans + account_entitlements tables."""

    def get_plan(self, plan_name: str) -> dict[str, Any] | None: ...
    def list_plans(self) -> list[dict[str, Any]]: ...
    def get_entitlements(self, user_id: str) -> dict[str, Any] | None: ...
    def get_entitlements_by_prefix(self, user_id_prefix: str) -> dict[str, Any] | None: ...
    def insert_entitlements_if_absent(self, row: dict[str, Any]) -> None: ...
    def update_entitlements(self, user_id: str, values: dict[str, Any]) -> None: ...


class PostgresEntitlementsStore:
    """EntitlementsStore backed by the connector's existing Neon DB."""

    def _plan_row_to_dict(self, row: tuple[Any, ...]) -> dict[str, Any]:
        return {"plan_name": row[0], **_quota_values_from_row(row, 1)}

    def _entitlements_row_to_dict(self, row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "user_id": row[0],
            "user_id_prefix": row[1],
            "plan_name": row[2],
            **_quota_values_from_row(row, 3),
        }

    def get_plan(self, plan_name: str) -> dict[str, Any] | None:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_PLAN_COLUMNS_SQL} FROM plans WHERE plan_name = %s", (plan_name,))
                row = cur.fetchone()
        finally:
            conn.close()
        return self._plan_row_to_dict(row) if row is not None else None

    def list_plans(self) -> list[dict[str, Any]]:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_PLAN_COLUMNS_SQL} FROM plans ORDER BY plan_name")
                rows = cur.fetchall()
        finally:
            conn.close()
        return [self._plan_row_to_dict(row) for row in rows]

    def get_entitlements(self, user_id: str) -> dict[str, Any] | None:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_ENTITLEMENT_COLUMNS_SQL} FROM account_entitlements WHERE user_id = %s",
                    (user_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return self._entitlements_row_to_dict(row) if row is not None else None

    def get_entitlements_by_prefix(self, user_id_prefix: str) -> dict[str, Any] | None:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_ENTITLEMENT_COLUMNS_SQL} FROM account_entitlements WHERE user_id_prefix = %s",
                    (user_id_prefix,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return self._entitlements_row_to_dict(row) if row is not None else None

    def insert_entitlements_if_absent(self, row: dict[str, Any]) -> None:
        column_names = ["user_id", "user_id_prefix", "plan_name", *QUOTA_ENTITLEMENT_NAMES]
        placeholders = ", ".join(["%s"] * len(column_names))
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO account_entitlements ({', '.join(column_names)}) "
                        f"VALUES ({placeholders}) ON CONFLICT (user_id) DO NOTHING",
                        tuple(row[name] for name in column_names),
                    )
        finally:
            conn.close()

    def update_entitlements(self, user_id: str, values: dict[str, Any]) -> None:
        allowed = {"plan_name", *QUOTA_ENTITLEMENT_NAMES}
        unknown = set(values) - allowed
        if unknown:
            raise UnknownEntitlementColumnError(sorted(unknown))
        assignments = ", ".join(f"{name} = %s" for name in values)
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE account_entitlements SET {assignments}, updated_at = NOW() WHERE user_id = %s",
                        (*values.values(), user_id),
                    )
        finally:
            conn.close()


@functools.cache
def get_entitlements_store() -> EntitlementsStore:
    return PostgresEntitlementsStore()


def _get_user_time_joined_ms(user_id: str, user_getter: Callable[[str], Any] = get_user) -> int:
    """Return the SuperTokens account-creation timestamp (epoch ms), 0 when unknown.

    An unknown timestamp (missing user, SDK error) conservatively counts as
    pre-existing -- the pre-cutoff rule only *adds* the paid-list check, and a
    genuinely-new account is never paid-listed by accident in practice.
    """
    try:
        user = user_getter(user_id)
    except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
        logger.warning("Failed to fetch SuperTokens user %s for time_joined: %s", user_id[:8], exc)
        return 0
    if user is None:
        return 0
    return int(user.time_joined)


def _initial_plan_name_for_user(
    user_id: str,
    email: str,
    # Resolved at call time (not bound as a default) so tests that patch the
    # module-level ``_get_user_time_joined_ms`` take effect.
    time_joined_getter: Callable[[str], int] | None = None,
    paid_checker: Callable[[str], bool] = is_email_paid,
) -> str:
    """Pick the plan for a lazily-created entitlements row.

    Accounts predating the feature-ship cutoff get ally when their email is
    paid-listed (the backfill rule); everyone else starts as explorer.
    """
    resolved_getter = time_joined_getter if time_joined_getter is not None else _get_user_time_joined_ms
    if resolved_getter(user_id) < _PREEXISTING_ACCOUNT_CUTOFF_EPOCH_MS and email and paid_checker(email):
        return PLAN_ALLY
    return PLAN_EXPLORER


def ensure_account_entitlements(
    user_id: str,
    user_id_prefix: str,
    email: str,
    store: "EntitlementsStore | None" = None,
) -> AccountEntitlements:
    """Return the account's entitlements row, lazily creating it from the initial plan.

    The lazy creation writes only the DB row; the LiteLLM user budget is
    pushed later, at the points that actually need it (`/keys/create` and the
    explicit plan/quota operations), so an unreachable LiteLLM cannot fail an
    unrelated request. Insert races resolve via ON CONFLICT DO NOTHING plus a
    re-read.
    """
    entitlements_store = store if store is not None else get_entitlements_store()
    existing = entitlements_store.get_entitlements(user_id)
    if existing is not None:
        return AccountEntitlements(**existing)
    plan_name = _initial_plan_name_for_user(user_id, email)
    plan = entitlements_store.get_plan(plan_name)
    if plan is None:
        raise PlanNotFoundError(plan_name)
    row = {
        "user_id": user_id,
        "user_id_prefix": user_id_prefix,
        "plan_name": plan_name,
        **{name: plan[name] for name in QUOTA_ENTITLEMENT_NAMES},
    }
    entitlements_store.insert_entitlements_if_absent(row)
    stored = entitlements_store.get_entitlements(user_id)
    if stored is None:
        raise HTTPException(status_code=500, detail="Failed to create the account entitlements row")
    return AccountEntitlements(**stored)


def resolve_entitlements_for_user(request: Request, user: UserAuth) -> AccountEntitlements:
    """Resolve (lazily creating) the entitlements row for a user-authenticated request."""
    token = request.headers.get("authorization", "")[7:]
    user_id = auth_module.get_user_id_from_access_token(token)
    return ensure_account_entitlements(user_id=user_id, user_id_prefix=user.user_id_prefix, email=user.email or "")


def raise_quota_exceeded(entitlement: str, limit: float, current: float, noun: str) -> NoReturn:
    raise QuotaExceededError(
        entitlement=entitlement,
        limit=limit,
        current=current,
        message=(
            f"Quota exceeded: this account allows {limit:g} {noun} and {current:g} are already in use. "
            "Free some up, or ask for a higher limit."
        ),
    )
