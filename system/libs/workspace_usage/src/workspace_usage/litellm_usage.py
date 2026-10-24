"""A LiteLLM callback that reports each direct model call an app makes.

``install_litellm_usage_callback()`` once at app start registers it for every
``litellm.completion`` / ``acompletion`` in the process, successes and
failures alike; the call itself is untouched. Needs the ``litellm`` extra.

A call's turn id is the one its ``metadata`` names (``TURN_ID_METADATA_KEY``),
else that of the ``usage_turn`` it was made in. The latter is read before the
request is sent, in the caller's own context, since LiteLLM may report a call
from a thread of its own.
"""

import os
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import litellm
from litellm.integrations.custom_logger import CustomLogger

from workspace_usage.context import read_usage_context, read_workspace_id
from workspace_usage.emit import record_model_call
from workspace_usage.events import (
    BILLING_MODE_API_KEY,
    BILLING_MODE_UNKNOWN,
    PURPOSE_APP_CALL,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_RATE_LIMITED,
    build_model_call,
)
from workspace_usage.turn import TURN_ID_METADATA_KEY, current_turn_id

_HARNESS = "litellm"
# Where the callback keeps the turn id it read before the request, in LiteLLM's per-call details.
_CAPTURED_TURN_ID_KEY = "imbue_usage_turn_id"
# Calls to these hosts are billed to the key the app passed; any other base (the LiteLLM
# proxy, a local gateway) bills someone the callback cannot see.
_PROVIDER_API_HOSTS = (
    "api.anthropic.com",
    "api.openai.com",
    "generativelanguage.googleapis.com",
)


class UsageCallback(CustomLogger):
    """Turns LiteLLM's per-call logging payload into a model_call event."""

    def __init__(
        self,
        context: Mapping[str, str],
        workspace_id: str,
        emit: Callable[[Mapping[str, object]], object] = record_model_call,
    ) -> None:
        super().__init__()
        self._context = dict(context)
        self._workspace_id = workspace_id
        self._emit = emit

    def log_pre_api_call(
        self, model: Any, messages: Any, kwargs: dict[str, Any]
    ) -> None:
        turn_id = current_turn_id()
        if turn_id is not None:
            kwargs[_CAPTURED_TURN_ID_KEY] = turn_id

    def log_success_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self._emit(
            self.model_call_event(
                kwargs, response_obj, start_time, end_time, is_success=True
            )
        )

    def log_failure_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self._emit(
            self.model_call_event(
                kwargs, response_obj, start_time, end_time, is_success=False
            )
        )

    async def async_log_success_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.log_success_event(kwargs, response_obj, start_time, end_time)

    async def async_log_failure_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.log_failure_event(kwargs, response_obj, start_time, end_time)

    def model_call_event(
        self,
        kwargs: Mapping[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
        is_success: bool,
    ) -> dict[str, object]:
        payload: Mapping[str, Any] = kwargs.get("standard_logging_object") or {}
        provider = payload.get("custom_llm_provider") or "unknown"
        tokens = _tokens(payload, response_obj)
        return build_model_call(
            event_id=str(
                payload.get("id") or kwargs.get("litellm_call_id") or uuid4().hex
            ),
            started_at=_as_utc(start_time),
            ended_at=_as_utc(end_time),
            provider=provider,
            model=payload.get("model") or kwargs.get("model"),
            context=self._context,
            workspace_id=self._workspace_id,
            tokens=tokens,
            cost_usd=_reported_cost(payload, tokens),
            billing_mode=_billing_mode(payload.get("api_base")),
            status=STATUS_OK
            if is_success
            else _failure_status(kwargs.get("exception")),
            harness=_HARNESS,
            purpose=PURPOSE_APP_CALL,
            turn_id=_turn_id(kwargs),
        )


def install_litellm_usage_callback(
    environ: Mapping[str, str] | None = None,
) -> UsageCallback:
    """Register a ``UsageCallback`` for this process's LiteLLM calls, attributed from its environment."""
    env = os.environ if environ is None else environ
    callback = UsageCallback(read_usage_context(env), read_workspace_id(env))
    litellm.callbacks.append(callback)
    return callback


def _turn_id(kwargs: Mapping[str, Any]) -> str | None:
    """The call's own metadata turn id, else the one captured before the request, else the current one."""
    litellm_params = kwargs.get("litellm_params")
    metadata = (
        litellm_params.get("metadata") if isinstance(litellm_params, Mapping) else None
    )
    candidates = (
        metadata.get(TURN_ID_METADATA_KEY) if isinstance(metadata, Mapping) else None,
        kwargs.get(_CAPTURED_TURN_ID_KEY),
        current_turn_id(),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _tokens(payload: Mapping[str, Any], response_obj: Any) -> dict[str, int | None]:
    """Non-overlapping buckets: LiteLLM's prompt_tokens includes cached input, which is split out here."""
    usage = getattr(response_obj, "usage", None)
    details = getattr(usage, "prompt_tokens_details", None)
    cache_read = (
        getattr(usage, "cache_read_input_tokens", None)
        or getattr(details, "cached_tokens", None)
        or 0
    )
    cache_write = getattr(usage, "cache_creation_input_tokens", None) or 0
    prompt_tokens = payload.get("prompt_tokens")
    completion_tokens = payload.get("completion_tokens")
    if prompt_tokens is None and completion_tokens is None:
        return {}
    return {
        "input": max(int(prompt_tokens or 0) - int(cache_read) - int(cache_write), 0),
        "output": int(completion_tokens or 0),
        "cache_read": int(cache_read),
        "cache_write": int(cache_write),
    }


def _reported_cost(
    payload: Mapping[str, Any], tokens: Mapping[str, int | None]
) -> Decimal | None:
    """LiteLLM's cost, except a $0 for a call that used tokens: that is an unpriced model, not a free one."""
    cost = payload.get("response_cost")
    if cost is None:
        return None
    if cost == 0 and any(tokens.values()):
        return None
    return Decimal(str(cost))


def _billing_mode(api_base: str | None) -> str:
    if not api_base or any(host in api_base for host in _PROVIDER_API_HOSTS):
        return BILLING_MODE_API_KEY
    return BILLING_MODE_UNKNOWN


def _failure_status(exception: object) -> str:
    return (
        STATUS_RATE_LIMITED
        if isinstance(exception, litellm.RateLimitError)
        else STATUS_ERROR
    )


def _as_utc(value: Any) -> datetime:
    """LiteLLM passes naive local datetimes (or epoch floats); events need aware UTC ones."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return datetime.now(timezone.utc)
