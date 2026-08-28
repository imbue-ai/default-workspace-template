"""Account-suspension state and the gates applied at session-creation paths.

Suspension enforcement is "no valid session, and no way to get one": the
operator suspend action (see ``suspension_admin.py``) sets the flag and
revokes every existing session, and this module's gate refuses each path that
would mint or refresh a session for a suspended account. Resource routes need
no separate check -- with the flag set, the account cannot present a live
session (state-modifying routes verify sessions against the SuperTokens core
per request, so revocation bites within one request).

The flag lives on ``account_entitlements`` (``suspended_at`` /
``suspended_reason``), orthogonal to plans and quota values, so unsuspending
restores the account exactly. An account with no entitlements row is not
suspended.
"""

import logging
from typing import Final

import imbue.remote_service_connector.entitlements as entitlements_module
from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector.errors import AccountSuspendedError

logger = logging.getLogger(__name__)

# The status string the browser auth surface answers with (the JSON `/auth/*`
# endpoints use the same value in their ``status`` field). Released clients
# treat unknown statuses as generic failures, so the value is additive.
ACCOUNT_SUSPENDED_STATUS: Final[str] = "ACCOUNT_SUSPENDED"

# What suspended users see, on every surface. Deliberately generic (the
# operator-recorded reason is internal) but actionable: a false positive can
# reach a human.
SUSPENDED_USER_MESSAGE: Final[str] = (
    "This account is suspended. If you believe this is a mistake, contact support@imbue.com."
)


def get_suspended_at(user_id: str, store: entitlements_module.EntitlementsStore | None = None) -> str | None:
    """Return when the account was suspended, or None when it is not suspended.

    Reads the account's entitlements row directly; a missing row means "not
    suspended" (rows are created lazily, and the suspend action always
    materializes one first).
    """
    entitlements_store = store if store is not None else entitlements_module.get_entitlements_store()
    row = entitlements_store.get_entitlements(user_id)
    if row is None:
        return None
    suspended_at = row.get("suspended_at")
    return str(suspended_at) if suspended_at else None


def is_user_suspended(user_id: str) -> bool:
    return get_suspended_at(user_id) is not None


def is_user_suspended_at_gate(user_id: str, gate: str) -> bool:
    """Whether the account is suspended, recording the refusal against ``gate``.

    Every session-creation path answers a suspended account in its own wire
    shape (a browser status, a JSON status, a redirect), so they ask this
    rather than raise; routing them all through here is what keeps blocked
    attempts countable per surface under one metric.
    """
    if not is_user_suspended(user_id):
        return False
    emit_metric("suspended_account_refused", 1, {"gate": gate})
    logger.info("Refused %s for suspended account %s", gate, user_id[:8])
    return True


def require_not_suspended(user_id: str, gate: str) -> None:
    """Refuse the request when the account is suspended (the raising gate).

    For paths whose refusal is the connector's structured 403; the paths that
    answer with their own status value call ``is_user_suspended_at_gate``.
    """
    if is_user_suspended_at_gate(user_id, gate):
        raise AccountSuspendedError(SUSPENDED_USER_MESSAGE)
