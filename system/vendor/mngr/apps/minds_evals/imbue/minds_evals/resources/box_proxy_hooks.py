"""The eval proxy's auth and usage recording, loaded by litellm from its config.

Runs inside the box, next to the proxy. Two jobs:

* **Auth.** The proxy runs with no database, so litellm's own virtual-key system is unavailable --
  and without either a master key or this, litellm grants internal-user rights to *any* key. A
  single per-trial key is issued by the driver and checked here, so a workspace that loses or alters
  its credential simply cannot reach a model.
* **Usage.** One JSON line per request, with the cache buckets kept separate. This is the metering
  boundary the transcript cannot provide: every agent in the workspace -- the chat agent, subagents,
  and separately created worker agents -- shares the workspace's credential, so all of their traffic
  lands here whether or not it ever appears in the graded transcript.

Configured through the environment rather than arguments, because litellm imports these by name.
"""

import hmac
import json
import os
import threading
from typing import Any

from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth

PROXY_KEY_ENV_VAR = "MINDS_EVAL_PROXY_KEY"
USAGE_LOG_ENV_VAR = "MINDS_EVAL_PROXY_USAGE_LOG"

_LOG_LOCK = threading.Lock()


class ProxyAuthError(Exception):
    """Raised when a request carries a key this proxy did not issue."""


def _expected_key() -> str:
    key = os.environ.get(PROXY_KEY_ENV_VAR, "")
    if not key:
        # Refusing everything is the safe failure: an unconfigured proxy that accepted requests
        # would meter nothing and still serve the model.
        raise ProxyAuthError("proxy key is not configured")
    return key


async def user_api_key_auth(request: Any, api_key: str) -> UserAPIKeyAuth:
    """Accept only the key the driver issued for this trial."""
    presented = (api_key or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(presented, _expected_key()):
        raise ProxyAuthError("invalid proxy key")
    return UserAPIKeyAuth(api_key=presented, key_alias="minds-eval-trial")


def _usage_fields(response_obj: Any) -> dict[str, Any]:
    """One response's usage as a plain mapping. litellm's usage is a pydantic model, so dumping it
    beats reaching for attributes that differ by provider and version."""
    if not hasattr(response_obj, "usage") or response_obj.usage is None:
        return {}
    usage = response_obj.usage
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    return dict(usage)


def _requested_speed(kwargs: dict[str, Any]) -> str | None:
    """Which speed tier this request was served at, or None for the standard one.

    Fast mode is a per-request parameter (``speed: "fast"``, gated behind a beta header) that trades
    a higher per-token price for throughput, so it has to be visible here or a trial's cost cannot be
    interpreted at all.

    Read from the *request* rather than the response on purpose. The response's ``usage.speed`` is
    the authoritative statement of what was served, but it does not survive to this callback:
    litellm normalizes the provider response into its OpenAI-shaped ``Usage``, which blanks ``speed``
    (it keeps ``service_tier``), and the raw body it would otherwise be recoverable from is retained
    only for non-streaming calls -- while real workspace traffic streams. ``optional_params`` is what
    litellm actually sent upstream, read *after* ``drop_params`` removes parameters the target model
    does not accept, and Anthropic rejects ``speed`` on an unsupported model rather than quietly
    serving it standard. So on a request that succeeded, this having survived means fast mode served
    it.
    """
    optional_params = kwargs.get("optional_params") or {}
    speed = optional_params.get("speed")
    return str(speed) if speed else None


def _cache_tokens(usage_fields: dict[str, Any]) -> tuple[int, int]:
    """(cache read, cache write), across the shapes litellm passes through.

    Anthropic reports both as top-level fields; the OpenAI-compatible shape nests reads under
    ``prompt_tokens_details.cached_tokens`` and reports no writes at all.
    """
    read = int(usage_fields.get("cache_read_input_tokens") or 0)
    write = int(usage_fields.get("cache_creation_input_tokens") or 0)
    if not read:
        details = usage_fields.get("prompt_tokens_details") or {}
        read = int((details or {}).get("cached_tokens") or 0)
    return read, write


class UsageLogger(CustomLogger):
    """Appends one record per completed request to the trial's proxy usage log."""

    async def async_log_success_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        log_path = os.environ.get(USAGE_LOG_ENV_VAR, "")
        if not log_path:
            return
        usage_fields = _usage_fields(response_obj)
        cache_read, cache_write = _cache_tokens(usage_fields)
        prompt_tokens = int(usage_fields.get("prompt_tokens") or 0)
        record = {
            "model": kwargs.get("model") or "",
            # Non-overlapping buckets, matching how the eval accounts for transcript usage: litellm
            # reports prompt_tokens *inclusive* of cache, so the cached portions come back out.
            # Leaving it inclusive would price cached tokens at the full input rate as well as the
            # cache rate.
            "input_tokens": max(0, prompt_tokens - cache_read - cache_write),
            "output_tokens": int(usage_fields.get("completion_tokens") or 0),
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            # Kept as reported so the normalization above can be re-derived from the record.
            "prompt_tokens_including_cache": prompt_tokens,
            # litellm's own cost for the call, priced from the model_list entry the driver
            # generated. Recorded alongside the tokens so the two can be reconciled. Note that the
            # model_list carries one price per model, so a fast-mode call is priced at the standard
            # rate it is not billed at -- hence recording the speed next to it.
            "cost_usd": kwargs.get("response_cost"),
            # "fast" or null. Always written (even when null) so that a log which simply predates
            # this field stays distinguishable from one that observed only standard-speed traffic.
            "speed": _requested_speed(kwargs),
            "call_type": kwargs.get("call_type") or "",
        }
        # The proxy serves requests concurrently, so serialize the appends.
        with _LOG_LOCK:
            with open(log_path, "a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(record) + "\n")


usage_logger = UsageLogger()
