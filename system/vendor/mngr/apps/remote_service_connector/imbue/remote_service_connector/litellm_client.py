"""Client for the LiteLLM proxy admin API, including per-account user budgets.


The monthly LLM spend quota is enforced by LiteLLM itself: every virtual key
carries the account's SuperTokens user_id, and LiteLLM aggregates spend from
all of a user's keys against the *user-level* ``max_budget``. The budget is
a rolling monthly window (``budget_duration = "1mo"``, anchored when the
budget is first created). Pushed at key-creation time and on every explicit
plan/quota change -- never during lazy row creation, so an unreachable
LiteLLM cannot fail an unrelated request.
"""

import logging
import os
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import HTTPException

from imbue.modal_app_kit.metrics import emit_metric

logger = logging.getLogger(__name__)


def litellm_proxy_url() -> str:
    """Return the LiteLLM proxy URL from environment. Raises 503 if not configured."""
    url = os.environ.get("LITELLM_PROXY_URL")
    if not url:
        raise HTTPException(status_code=503, detail="LiteLLM proxy not configured")
    return url.rstrip("/")


def litellm_master_key() -> str:
    """Return the LiteLLM master key from environment. Raises 503 if not configured."""
    key = os.environ.get("LITELLM_MASTER_KEY")
    if not key:
        raise HTTPException(status_code=503, detail="LiteLLM master key not configured")
    return key


def litellm_request(
    method: str,
    path: str,
    json_body: dict[str, object] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """Make an authenticated request to the LiteLLM proxy admin API."""
    url = litellm_proxy_url() + path
    headers = {"Authorization": "Bearer {}".format(litellm_master_key())}
    response = httpx.request(
        method=method,
        url=url,
        headers=headers,
        json=json_body,
        params=params,
        timeout=60.0,
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        # Counted rather than warned: some rejections are routine (e.g.
        # /user/new for an existing user on every key mint), and the raised
        # HTTPException already surfaces the genuine failures to the caller.
        emit_metric("litellm_api_error", 1, {"path": path, "status": str(response.status_code)})
        logger.info("LiteLLM API error: %s %s -> %s %s", method, path, response.status_code, detail)
        raise HTTPException(status_code=response.status_code, detail="LiteLLM error: {}".format(detail))
    return response


def litellm_base_url_for_agents() -> str:
    """Return the base URL agents should use as ANTHROPIC_BASE_URL."""
    return litellm_proxy_url()


def list_litellm_user_key_entries(user_id: str) -> list[Any]:
    """Return every virtual key LiteLLM holds for the account, as full objects.

    Without ``return_full_object=true`` LiteLLM answers with a bare list of
    token-id strings; with it, each entry is a dict carrying token / alias /
    spend / budget. The response is either the list itself or a ``keys``
    envelope depending on the proxy version, so both shapes are unwrapped
    here. Entries are returned unfiltered -- callers project what they need
    and decide how to treat a non-dict entry.
    """
    response = litellm_request("GET", "/key/list", params={"user_id": user_id, "return_full_object": "true"})
    data = response.json()
    return data if isinstance(data, list) else data.get("keys", [])


_LITELLM_USER_BUDGET_DURATION = "1mo"


def upsert_litellm_user_budget(user_id: str, max_budget: float) -> None:
    """Create or update the LiteLLM internal user carrying the account's monthly budget.

    Raises (via ``litellm_request``) on failure -- callers deliberately let
    that fail the whole operation so the DB row and LiteLLM never diverge.
    """
    body: dict[str, object] = {
        "user_id": user_id,
        "max_budget": max_budget,
        "budget_duration": _LITELLM_USER_BUDGET_DURATION,
    }
    try:
        litellm_request("POST", "/user/new", json_body=body)
    except HTTPException as exc:
        # LiteLLM rejects /user/new for an existing user (the exact status/text
        # varies by version); fall through to /user/update, which raises on any
        # genuine failure.
        logger.debug("LiteLLM /user/new for %s rejected (%s); trying /user/update", user_id[:8], exc.status_code)
        litellm_request("POST", "/user/update", json_body=body)


def get_litellm_user_spend(
    user_id: str,
    # Resolved at call time (not bound as a default) so installed fakes that
    # replace the module-level ``litellm_request`` still take effect.
    request_fn: "Callable[..., httpx.Response] | None" = None,
) -> tuple[float, str | None]:
    """Return (spend this budget period, budget reset timestamp) for the account.

    A user that does not exist in LiteLLM yet (never minted a key) reports
    zero spend. Any LiteLLM error also reports zero -- this feeds the
    display-only usage endpoint, not enforcement. ``request_fn`` is injected
    for tests; production callers use the module-level ``litellm_request``.
    """
    resolved_request = request_fn if request_fn is not None else litellm_request
    try:
        response = resolved_request("GET", "/user/info", params={"user_id": user_id})
    except (HTTPException, httpx.HTTPError) as exc:
        # HTTPException covers HTTP >= 400 responses and missing proxy config;
        # httpx.HTTPError covers transport failures (proxy unreachable).
        # Counts the degraded outcome (spend reported as zero); the HTTP >= 400
        # path is additionally counted inside litellm_request with its status.
        emit_metric("litellm_spend_read_failed", 1, {})
        logger.warning("LiteLLM /user/info for %s failed; reporting zero spend", user_id[:8], exc_info=exc)
        return 0.0, None
    data = response.json()
    info = data.get("user_info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return 0.0, None
    spend = info.get("spend")
    reset_at = info.get("budget_reset_at")
    return (float(spend) if spend is not None else 0.0, str(reset_at) if reset_at else None)
