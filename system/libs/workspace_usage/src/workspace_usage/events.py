"""Building ``model_call`` events (schema version 1 of ``imbue.mngr_usage.model_call``) as JSON-ready dicts.

Writers report per-call amounts. A writer that knows tokens but not dollars
leaves ``cost_usd`` out and the collector prices the call; a writer that
knows neither still reports the call, so it is counted with an unknown cost.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Final

from workspace_usage.context import ORIGIN_KIND, ORIGIN_KIND_UNKNOWN

MODEL_CALL_SCHEMA_VERSION: Final[int] = 1

BILLING_MODE_API_KEY: Final[str] = "API_KEY"
BILLING_MODE_IMBUE_KEY: Final[str] = "IMBUE_KEY"
BILLING_MODE_SUBSCRIPTION: Final[str] = "SUBSCRIPTION"
BILLING_MODE_UNKNOWN: Final[str] = "UNKNOWN"

STATUS_OK: Final[str] = "OK"
STATUS_ERROR: Final[str] = "ERROR"
STATUS_RATE_LIMITED: Final[str] = "RATE_LIMITED"
STATUS_CANCELLED: Final[str] = "CANCELLED"

PURPOSE_CHAT_TITLE: Final[str] = "CHAT_TITLE"
PURPOSE_APP_CALL: Final[str] = "APP_CALL"

TOKEN_BUCKETS: Final[tuple[str, ...]] = (
    "input",
    "output",
    "cache_read",
    "cache_write",
    "cache_write_1h",
)


def build_model_call(
    *,
    event_id: str,
    started_at: datetime,
    ended_at: datetime,
    provider: str,
    model: str | None,
    context: Mapping[str, str],
    workspace_id: str,
    tokens: Mapping[str, int | None] | None = None,
    cost_usd: Decimal | None = None,
    billing_mode: str = BILLING_MODE_UNKNOWN,
    status: str = STATUS_OK,
    harness: str | None = None,
    purpose: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    credential_ref: str | None = None,
    modifiers: Sequence[str] = (),
) -> dict[str, object]:
    """One model_call event. ``model`` is the bare model id; it is qualified with ``provider`` here."""
    attribution: dict[str, object] = {
        "workspace_id": workspace_id,
        ORIGIN_KIND: context.get(ORIGIN_KIND, ORIGIN_KIND_UNKNOWN),
        **{
            key: value for key, value in context.items() if key != ORIGIN_KIND and value
        },
        "harness": harness,
        "purpose": purpose,
        "session_id": session_id,
        "turn_id": turn_id,
    }
    event: dict[str, object] = {
        "schema_version": MODEL_CALL_SCHEMA_VERSION,
        "event_id": event_id,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "provider": provider,
        "model": qualify_model(provider, model),
        "tokens": {
            bucket: count
            for bucket, count in (tokens or {}).items()
            if bucket in TOKEN_BUCKETS
        },
        "modifiers": list(modifiers),
        "cost_usd": str(cost_usd) if cost_usd is not None else None,
        "cost_provenance": "REPORTED" if cost_usd is not None else None,
        "billing_mode": billing_mode,
        "credential_ref": credential_ref,
        "status": status,
        "attribution": {
            key: value for key, value in attribution.items() if value is not None
        },
    }
    return {key: value for key, value in event.items() if value is not None}


def qualify_model(provider: str, model: str | None) -> str | None:
    """The canonical ``<provider>/<model>`` key, leaving an already-qualified id alone."""
    if model is None or not model.strip():
        return None
    model = model.strip()
    return model if model.startswith(f"{provider}/") else f"{provider}/{model}"
