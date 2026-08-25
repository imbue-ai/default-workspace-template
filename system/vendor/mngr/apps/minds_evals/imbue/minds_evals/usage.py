"""Token and cost accounting for a trial, split by who spent it.

Two different LLM consumers run during a trial and they must not be conflated:

- the **workspace agent** under test, whose consumption is the eval's subject. Its per-message usage
  already rides the workspace event stream: the workspace's ``system_interface`` parses claude's
  session files itself (its ``AgentSessionWatcher``, which reimplements mngr's common_transcript
  conversion) and attaches a ``usage`` block and a ``model`` to every ``assistant_message``. So
  nothing has to be collected out of the workspace before it is destroyed -- the driver's own
  transcript is the source.
- the **decider**, the harness's simulated-user model. It is a cost of running the eval, not a
  property of the thing being measured, so it is reported separately as metadata.

**What this source does not see: delegated work.** The events endpoint serves main-session events
only -- a subagent's turns are deliberately routed to a separate per-subagent stream so they do not
render inline in the parent thread -- and work handed to a freshly created mngr worker agent belongs
to that agent's stream entirely. Neither one's tokens reach the sum below, so an agent that delegates
looks cheaper than one that does the same work inline, which would make cost gameable. Until that
usage is captured, delegation is at least *detected*: any trial that delegates is marked
``is_cost_complete = False`` rather than quietly reporting a clean total.

Both are priced with ``mngr_usage``'s table rather than a local copy, so these numbers stay bound to
the prices the LiteLLM proxy bills at (``mngr_usage``'s litellm_pricing_test pins that table to
litellm's own price map, which is what the proxy bills from).

**Speed tier and what it does to cost.** Fast mode bills the same tokens at twice the standard rate
($10/$50 per MTok against $5/$25 on Opus 5 and Opus 4.8), and it is chosen per request, so a model id
alone does not determine a price. Token counts are unaffected; only the rate applied to them is. The
proxy records the tier per request and each tier's tokens are then priced at its own rate, so a trial
that ran fast reports what it actually cost. A source that cannot see the tier -- the transcript --
prices everything standard and says so through ``is_cost_rate_certain``: that figure is a floor, and
half the truth if the workspace was in fast mode, which by default it is.

Token buckets follow ``TokenSnapshot``'s non-overlapping convention: ``input`` counts only
non-cached input, with cache reads and cache writes kept separate, because Anthropic prices the
three differently (a cache write costs 1.25x a plain input token, a cache read 0.1x). Collapsing
them into one "input" number cannot produce a correct cost, and hides the cache behaviour that
cache-aware compaction work needs to see.
"""

import json
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals.data_types import DeciderResult
from imbue.mngr_usage.data_types import TokenSnapshot
from imbue.mngr_usage.pricing import compute_cost

# The workspace event stream reports a bare model id ("claude-opus-4-8"); the pricing table is keyed
# by "<provider>/<model>". Only claude ids can be resolved to a provider by name -- anything else is
# reported unpriced rather than guessed at, so a harness that starts reporting a different model
# shows up as a missing price instead of a silently wrong cost.
_ANTHROPIC_PREFIX: Final[str] = "anthropic/"
_CLAUDE_MODEL_PREFIX: Final[str] = "claude"

# The workspace transcript's usage keys, which the common_transcript converter renames from
# Anthropic's wire names (cache_creation_input_tokens -> cache_write_tokens, and so on).
_INPUT_KEY: Final[str] = "input_tokens"
_OUTPUT_KEY: Final[str] = "output_tokens"
_CACHE_READ_KEY: Final[str] = "cache_read_tokens"
_CACHE_WRITE_KEY: Final[str] = "cache_write_tokens"

# Claude Code's delegation tool. Current versions name it "Agent"; "Task" is the older name, kept so
# a transcript captured against an older pinned CLI is still recognized.
_DELEGATION_TOOL_NAMES: Final[frozenset[str]] = frozenset({"Agent", "Task"})
# The other delegation route: dwt's launch-task skill shells out to create a separate mngr worker
# agent, which produces no Agent tool call at all -- it is an ordinary Bash command. Matching on the
# command text is a heuristic, so it is reported as its own count rather than merged with the exact
# one above. Only the launching subcommand counts: the same script's `await` and status calls refer
# back to a worker already counted, and would otherwise inflate one delegation into several.
_WORKER_LAUNCH_MARKERS: Final[tuple[str, ...]] = ("create_worker.py launch", "mngr create")

# The proxy log's value for a request served in fast mode; standard-speed requests record null.
_FAST_SPEED: Final[str] = "fast"
_SPEED_KEY: Final[str] = "speed"


@pure
def canonical_model_key(model: str) -> str | None:
    """The pricing-table key for a transcript's model id, or None when the provider is unknown.

    An id that already carries a provider prefix is taken as-is; a bare claude id is Anthropic's.
    Returning None (rather than a guess) is what makes an unpriceable model visible downstream.
    """
    if not model:
        return None
    if "/" in model:
        return model
    if model.startswith(_CLAUDE_MODEL_PREFIX):
        return _ANTHROPIC_PREFIX + model
    return None


@pure
def _token_snapshot(raw_usage: Mapping[str, Any]) -> TokenSnapshot:
    """One message's usage block as a TokenSnapshot, treating absent counters as zero."""
    return TokenSnapshot(
        input=int(raw_usage.get(_INPUT_KEY) or 0),
        output=int(raw_usage.get(_OUTPUT_KEY) or 0),
        cache_read=int(raw_usage.get(_CACHE_READ_KEY) or 0),
        cache_creation=int(raw_usage.get(_CACHE_WRITE_KEY) or 0),
    )


@pure
def _has_any_tokens(tokens: TokenSnapshot) -> bool:
    return bool(
        (tokens.input or 0) or (tokens.output or 0) or (tokens.cache_read or 0) or (tokens.cache_creation or 0)
    )


@pure
def _add(left: TokenSnapshot, right: TokenSnapshot) -> TokenSnapshot:
    return TokenSnapshot(
        input=(left.input or 0) + (right.input or 0),
        output=(left.output or 0) + (right.output or 0),
        cache_read=(left.cache_read or 0) + (right.cache_read or 0),
        cache_creation=(left.cache_creation or 0) + (right.cache_creation or 0),
    )


class ModelUsage(FrozenModel):
    """What one model consumed over a trial, and what that cost."""

    model: str = Field(description="Model id as the transcript reported it")
    pricing_key: str | None = Field(description="Pricing-table key, or None when the provider is unknown")
    message_count: int = Field(description="Agent messages attributed to this model")
    tokens: TokenSnapshot = Field(description="Non-overlapping token buckets for this model")
    cost_usd: float | None = Field(description="USD cost, or None when the model is not in the pricing table")
    fast_message_count: int = Field(
        default=0, description="Of message_count, those served in fast mode (0 when speed is unobserved)"
    )
    fast_tokens: TokenSnapshot = Field(
        default_factory=TokenSnapshot,
        description="The portion of `tokens` spent in fast mode -- a subset, not an addition",
    )


class TrialUsage(FrozenModel):
    """The workspace agent's total consumption for one trial, per model and in aggregate."""

    per_model: tuple[ModelUsage, ...] = Field(description="One entry per model seen, ordered by first appearance")
    tokens: TokenSnapshot = Field(description="Token buckets summed across models")
    cost_usd: float | None = Field(
        description="USD across all models, or None when any model is unpriced (a partial total would understate it)"
    )
    message_count: int = Field(description="Agent messages carrying usage")
    unpriced_models: tuple[str, ...] = Field(description="Models seen with no entry in the pricing table")
    delegated_call_count: int = Field(description="Subagent (Agent tool) calls, whose usage this total excludes")
    worker_launch_count: int = Field(
        description="Bash commands that look like a worker-agent launch, whose usage this total also excludes"
    )
    is_speed_observed: bool = Field(
        default=False,
        description="Whether this source can see which speed tier served each request (only the proxy can)",
    )
    fast_message_count: int = Field(default=0, description="Requests served in fast mode across all models")
    fast_tokens: TokenSnapshot = Field(
        default_factory=TokenSnapshot, description="The portion of `tokens` spent in fast mode -- a subset"
    )

    @property
    def is_cost_rate_certain(self) -> bool:
        """Whether ``cost_usd`` was computed at the rate the traffic was actually billed at.

        Fast mode bills the same tokens at twice the standard rate, so the rate is only known when
        the tier was. When it was, each portion is priced at its own tier and the total is exact
        whether or not any of it ran fast. When it was not -- the transcript, which carries no speed
        information, or a proxy log predating speed recording -- everything is priced standard, which
        is a floor: correct if the trial happened to run entirely standard, and half the truth if it
        did not.

        Separate axis from ``is_cost_complete``: that one asks whether *all the traffic* is in the
        total, this one whether the traffic in it is *priced correctly*.
        """
        return self.is_speed_observed

    @property
    def is_cost_complete(self) -> bool:
        """Whether the total accounts for all the work the agent caused.

        False once the agent delegates: subagent turns are served on a separate stream and a worker
        agent has a stream of its own, so neither's tokens are in this sum. A trial that delegates
        therefore looks cheaper than one doing the same work inline -- consumers must not compare the
        two as if both were complete.
        """
        return self.delegated_call_count == 0 and self.worker_launch_count == 0

    @property
    def n_input_tokens(self) -> int:
        """Harbor's cache-inclusive input count: every input token, cached or not."""
        return (self.tokens.input or 0) + (self.tokens.cache_read or 0) + (self.tokens.cache_creation or 0)

    @property
    def n_cache_tokens(self) -> int:
        """Harbor's cache count, read as tokens *served from* the cache.

        Harbor documents this only as "the number of cache tokens used", which does not say whether
        writes belong in it. Reads alone is the reading that keeps ``n_cache_tokens / n_input_tokens``
        meaningful as a cache hit rate; the unambiguous four-way split lives in the trial metadata.
        """
        return self.tokens.cache_read or 0


class DeciderUsage(FrozenModel):
    """What the harness's simulated-user model consumed. Reported as metadata, never as the
    agent's own usage: it measures the cost of running the eval, not the agent under test."""

    model: str = Field(description="Decider model")
    call_count: int = Field(description="Decider calls made")
    fallback_count: int = Field(description="Calls that fell back to the literal message")
    input_token_count: int = Field(description="Input tokens across decider calls")
    output_token_count: int = Field(description="Output tokens across decider calls")
    cost_usd: float | None = Field(description="USD cost, or None when the model is not in the pricing table")


@pure
def _tiered_cost(pricing_key: str | None, standard_tokens: TokenSnapshot, fast_tokens: TokenSnapshot) -> float | None:
    """One model's cost with each tier's tokens priced at that tier's rate.

    None if either portion is unpriceable, because a partial sum reads as a complete cost. A model
    that served fast-mode traffic but has no fast-mode price is the case worth being strict about:
    pricing it standard would halve a real bill.
    """
    if pricing_key is None:
        return None
    standard_cost = compute_cost(pricing_key, standard_tokens)
    if standard_cost is None:
        return None
    if not _has_any_tokens(fast_tokens):
        return standard_cost
    fast_cost = compute_cost(pricing_key, fast_tokens, is_fast_mode=True)
    if fast_cost is None:
        return None
    return standard_cost + fast_cost


@pure
def _count_delegations(raw_tool_calls: Any) -> tuple[int, int]:
    """(subagent calls, worker launches) in one message's tool calls."""
    if not isinstance(raw_tool_calls, Sequence) or isinstance(raw_tool_calls, (str, bytes)):
        return 0, 0
    delegated = 0
    launched = 0
    for tool_call in raw_tool_calls:
        if not isinstance(tool_call, Mapping):
            continue
        tool_name = str(tool_call.get("tool_name") or "")
        if tool_name in _DELEGATION_TOOL_NAMES:
            delegated += 1
            continue
        preview = str(tool_call.get("input_preview") or "")
        if any(marker in preview for marker in _WORKER_LAUNCH_MARKERS):
            launched += 1
    return delegated, launched


@pure
def summarize_workspace_usage(events: Sequence[Mapping[str, Any]]) -> TrialUsage:
    """Aggregate the workspace agent's usage out of the raw chat event stream.

    Agent messages without a usage block are skipped rather than counted as zero, so a transcript
    whose converter predates usage reporting yields an empty summary instead of a confident zero.

    The stream reports token counts but not which speed tier served them, so the result leaves
    ``is_speed_observed`` false: fast mode is only visible to the proxy, which sees the request
    parameter itself.
    """
    tokens_by_model: dict[str, TokenSnapshot] = {}
    messages_by_model: dict[str, int] = {}
    delegated_call_count = 0
    worker_launch_count = 0
    previous_usage: tuple[str, int, int, int, int] | None = None
    for event in events:
        if event.get("type") != "assistant_message":
            continue
        delegated, launched = _count_delegations(event.get("tool_calls"))
        delegated_call_count += delegated
        worker_launch_count += launched
        raw_usage = event.get("usage")
        if not isinstance(raw_usage, Mapping) or not raw_usage:
            continue
        tokens = _token_snapshot(raw_usage)
        # One API response becomes several transcript messages -- one per content block -- and each
        # carries a copy of the response's usage, so summing them all counts the same tokens two or
        # three times. Consecutive messages reporting identical usage are those blocks. Verified
        # against a proxy metering the same trial: collapsing them reproduces the billed cost
        # exactly, while summing every message overstated it by nearly a factor of two.
        model = str(event.get("model") or "")
        # The model is part of the identity: blocks of one response always share it, so two
        # different models reporting the same counts are two responses, not one.
        signature = (
            model,
            tokens.input or 0,
            tokens.output or 0,
            tokens.cache_read or 0,
            tokens.cache_creation or 0,
        )
        if signature == previous_usage:
            continue
        previous_usage = signature
        if not _has_any_tokens(tokens):
            # Claude Code emits synthetic messages -- the pre-sign-in "Not logged in" notice, for
            # one -- under a `<synthetic>` model with an all-zero usage block. They cost nothing, so
            # counting them would only let an unpriceable pseudo-model void an otherwise complete
            # trial cost.
            continue
        tokens_by_model[model] = _add(tokens_by_model.get(model, TokenSnapshot()), tokens)
        messages_by_model[model] = messages_by_model.get(model, 0) + 1

    per_model: list[ModelUsage] = []
    for model, tokens in tokens_by_model.items():
        pricing_key = canonical_model_key(model)
        per_model.append(
            ModelUsage(
                model=model,
                pricing_key=pricing_key,
                message_count=messages_by_model[model],
                tokens=tokens,
                cost_usd=compute_cost(pricing_key, tokens) if pricing_key is not None else None,
            )
        )

    total_tokens = TokenSnapshot()
    for entry in per_model:
        total_tokens = _add(total_tokens, entry.tokens)
    unpriced = tuple(entry.model for entry in per_model if entry.cost_usd is None)
    return TrialUsage(
        per_model=tuple(per_model),
        tokens=total_tokens,
        # None rather than 0.0 in both unknown cases: summing only the priced models would report a
        # total that looks complete and is not, and a trial with no usage at all did not cost zero --
        # we simply do not know what it cost.
        cost_usd=None if (unpriced or not per_model) else sum(entry.cost_usd or 0.0 for entry in per_model),
        message_count=sum(messages_by_model.values()),
        unpriced_models=unpriced,
        delegated_call_count=delegated_call_count,
        worker_launch_count=worker_launch_count,
    )


@pure
def summarize_decider_usage(results: Sequence[DeciderResult], model: str) -> DeciderUsage:
    """Aggregate the decider's own calls. ``model`` is the configured decider model, used when a
    result carries none of its own (a fallback records no model)."""
    input_token_count = sum(result.input_token_count for result in results)
    output_token_count = sum(result.output_token_count for result in results)
    pricing_key = canonical_model_key(model)
    tokens = TokenSnapshot(input=input_token_count, output=output_token_count)
    return DeciderUsage(
        model=model,
        call_count=len(results),
        fallback_count=sum(1 for result in results if result.is_fallback),
        input_token_count=input_token_count,
        output_token_count=output_token_count,
        cost_usd=compute_cost(pricing_key, tokens) if pricing_key is not None else None,
    )


@pure
def _token_dict(tokens: TokenSnapshot) -> dict[str, int]:
    return {
        "input": tokens.input or 0,
        "output": tokens.output or 0,
        "cache_read": tokens.cache_read or 0,
        "cache_write": tokens.cache_creation or 0,
    }


@pure
def workspace_usage_metadata(usage: TrialUsage) -> dict[str, Any]:
    """The workspace agent's usage as trial metadata: the four-way split harbor's own fields cannot
    express, plus the per-model breakdown an A/B of routing arms needs."""
    return {
        "message_count": usage.message_count,
        "tokens": _token_dict(usage.tokens),
        "cost_usd": usage.cost_usd,
        "unpriced_models": list(usage.unpriced_models),
        # False means the agent delegated and this total excludes that work -- see TrialUsage.
        "is_cost_complete": usage.is_cost_complete,
        "delegated_call_count": usage.delegated_call_count,
        "worker_launch_count": usage.worker_launch_count,
        # Which speed tier served the traffic, and therefore whether cost_usd is priced at the rate
        # it was billed at -- see TrialUsage.is_cost_rate_certain. The transcript carries no speed
        # information, so these stay false/zero unless the trial ran with the proxy.
        "is_speed_observed": usage.is_speed_observed,
        "is_cost_rate_certain": usage.is_cost_rate_certain,
        "fast_message_count": usage.fast_message_count,
        "fast_tokens": _token_dict(usage.fast_tokens),
        "per_model": [
            {
                "model": entry.model,
                "message_count": entry.message_count,
                "tokens": _token_dict(entry.tokens),
                "cost_usd": entry.cost_usd,
                "fast_message_count": entry.fast_message_count,
                "fast_tokens": _token_dict(entry.fast_tokens),
            }
            for entry in usage.per_model
        ],
    }


@pure
def decider_usage_metadata(usage: DeciderUsage) -> dict[str, Any]:
    return {
        "model": usage.model,
        "call_count": usage.call_count,
        "fallback_count": usage.fallback_count,
        "tokens": {"input": usage.input_token_count, "output": usage.output_token_count},
        "cost_usd": usage.cost_usd,
    }


@pure
def summarize_proxy_usage(records: Sequence[Mapping[str, Any]]) -> TrialUsage:
    """Aggregate the proxy's per-request log.

    This is the complete account of what a trial spent, and the only one that is: every agent in the
    workspace shares the credential the proxy issued, so a subagent's or worker's calls arrive here
    even though they never appear in the chat agent's transcript. Measured on a delegating case, the
    transcript saw 44 responses and the proxy 69 -- 45% of the real cost was invisible.

    The records already carry non-overlapping buckets, normalized by the proxy's own logger.
    """
    # Kept apart by tier all the way through, because the two are billed at different rates and
    # summing them first would leave nothing to apply the right rate to.
    standard_tokens_by_model: dict[str, TokenSnapshot] = {}
    fast_tokens_by_model: dict[str, TokenSnapshot] = {}
    requests_by_model: dict[str, int] = {}
    fast_requests_by_model: dict[str, int] = {}
    for record in records:
        model = str(record.get("model") or "")
        tokens = TokenSnapshot(
            input=int(record.get("input_tokens") or 0),
            output=int(record.get("output_tokens") or 0),
            cache_read=int(record.get("cache_read_tokens") or 0),
            cache_creation=int(record.get("cache_write_tokens") or 0),
        )
        requests_by_model[model] = requests_by_model.get(model, 0) + 1
        if record.get(_SPEED_KEY) == _FAST_SPEED:
            fast_tokens_by_model[model] = _add(fast_tokens_by_model.get(model, TokenSnapshot()), tokens)
            fast_requests_by_model[model] = fast_requests_by_model.get(model, 0) + 1
        else:
            standard_tokens_by_model[model] = _add(standard_tokens_by_model.get(model, TokenSnapshot()), tokens)

    per_model: list[ModelUsage] = []
    for model in requests_by_model:
        pricing_key = canonical_model_key(model)
        standard_tokens = standard_tokens_by_model.get(model, TokenSnapshot())
        fast_tokens = fast_tokens_by_model.get(model, TokenSnapshot())
        per_model.append(
            ModelUsage(
                model=model,
                pricing_key=pricing_key,
                message_count=requests_by_model[model],
                tokens=_add(standard_tokens, fast_tokens),
                cost_usd=_tiered_cost(pricing_key, standard_tokens, fast_tokens),
                fast_message_count=fast_requests_by_model.get(model, 0),
                fast_tokens=fast_tokens,
            )
        )
    total_tokens = TokenSnapshot()
    total_fast_tokens = TokenSnapshot()
    for entry in per_model:
        total_tokens = _add(total_tokens, entry.tokens)
        total_fast_tokens = _add(total_fast_tokens, entry.fast_tokens)
    unpriced = tuple(entry.model for entry in per_model if entry.cost_usd is None)
    return TrialUsage(
        per_model=tuple(per_model),
        tokens=total_tokens,
        cost_usd=None if (unpriced or not per_model) else sum(entry.cost_usd or 0.0 for entry in per_model),
        message_count=sum(requests_by_model.values()),
        unpriced_models=unpriced,
        # Nothing is missing from this source: it is the boundary every call crosses, so delegated
        # work is already included rather than merely detected.
        delegated_call_count=0,
        worker_launch_count=0,
        # Every record must carry the key, not merely some of them: a log written before the proxy
        # recorded speed reports no fast requests for the same reason a genuinely all-standard trial
        # does, and only the key's presence separates the two.
        is_speed_observed=bool(records) and all(_SPEED_KEY in record for record in records),
        fast_message_count=sum(entry.fast_message_count for entry in per_model),
        fast_tokens=total_fast_tokens,
    )


@pure
def parse_proxy_usage_log(contents: str) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    for line in contents.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        records.append(json.loads(stripped))
    return tuple(records)
