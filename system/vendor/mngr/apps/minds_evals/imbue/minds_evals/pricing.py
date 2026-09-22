"""Token -> USD pricing, read from litellm's own price map at the moment a report is built.

Nothing a trial records is priced. A trial writes token counts per model, and every figure in a
report is derived from them here, so a provider changing a price cannot rewrite trial data: the same
tokens re-priced later simply produce a different number, and the number says which map produced it
(``PriceMap.pricing_source``).

The map is litellm's ``model_cost``, the same table the LiteLLM proxy bills from, rather than a copy
of it maintained here. litellm loads it at import: normally by fetching its own published map over
the network, falling back to the copy bundled in the wheel when that fails. Those two differ -- the
bundled copy of 1.93.0 carries no ``claude-opus-5``, which the default arm runs on, and no
``openrouter/moonshotai/kimi-k2.6`` -- which is why ``PricingSource`` records which of them answered.

The map reaches a lookup as a ``PriceMap`` argument, read from litellm's globals in exactly one place
(``load_litellm_price_map``) and carried from there. A report is therefore priced from one map, the
one it names, and a caller that needs rates it chose itself passes a table of its own.

A reported model id is not always a key the map holds, either: a harness reports the model without the
gateway that billed it, so a lookup also takes the catalog id the trial asked for and matches the two
(``resolve_price_entry``).

Three things decide a rate beyond the model id, and each one has to arrive with the tokens:

- **The bucket.** A cache read costs a tenth of a fresh input token and a cache write more than one,
  so ``TokenSnapshot``'s buckets are non-overlapping and each is priced at its own rate.
- **The speed tier.** Fast mode returns the same tokens faster for twice the price and is chosen per
  request, so a model id alone does not determine a price.
- **The cache TTL.** Anthropic bills a cache write by how long it lives, and the map states the
  1-hour rate separately where a model has one.
"""

from collections.abc import Mapping
from enum import auto
from importlib.metadata import version
from typing import Any
from typing import Final
from typing import assert_never

import litellm
from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map_source_info
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals.data_types import PricingSource
from imbue.minds_evals.data_types import TokenSnapshot

# The one prefix that is stripped before a second lookup. litellm keys every claude model by its bare
# id ("claude-opus-4-8"), which is what the harnesses report; a record that names Anthropic alongside
# it ("anthropic/claude-opus-5", the form the proxy forwards upstream) has to reach the same rates. No
# other prefix is rewritten: which gateway served a model is otherwise the trial's own record to say.
_ANTHROPIC_PREFIX: Final[str] = "anthropic/"

# litellm's name for the rate a prompt-cache write costs when it was asked to live an hour. Absent
# from the entries of models that bill one cache-write rate whatever the TTL.
_ABOVE_1HR_KEY: Final[str] = "cache_creation_input_token_cost_above_1hr"

LITELLM_SOURCE: Final[str] = "litellm"

# Fast mode bills the same tokens at twice the standard rate ($10/$50 per MTok against $5/$25 on
# Opus), across the full context window. It is a flat multiplier rather than a second set of rates
# because it doubles *every* bucket: the cache rates are defined against the input rate (a write
# costs 1.25x an input token, a read 0.1x), so doubling that carries them along.
FAST_MODE_PRICE_MULTIPLIER: Final[float] = 2.0
# Which models can serve a request in fast mode, keyed the way litellm keys them. This is not a
# property of a price entry because the two do not partition the same way: one set of rates is shared
# across the whole Opus generation, but only these members of it offer the tier. The API rejects
# `speed` outright on Sonnet and Haiku, and runs Opus 4.6 and older at standard speed and rates.
FAST_MODE_MODELS: Final[frozenset[str]] = frozenset({"claude-opus-5", "claude-opus-4-8"})

# The harness a Claude Code chat runs under, as the workspace's accounts listing names it.
CLAUDE_HARNESS: Final[str] = "claude"


class CacheWriteTtl(UpperCaseStrEnum):
    """How long a prompt-cache write was asked to live, which is what decides its rate."""

    FIVE_MINUTES = auto()
    ONE_HOUR = auto()


class PriceEntry(FrozenModel):
    """One model's per-token rates, as litellm's price map states them."""

    litellm_key: str = Field(description="The key litellm's map holds these rates under")
    input_cost_per_token: float = Field(description="USD per input token served fresh")
    output_cost_per_token: float = Field(description="USD per output token, reasoning tokens included")
    cache_read_input_token_cost: float = Field(description="USD per input token served from the prompt cache")
    cache_creation_input_token_cost: float = Field(description="USD per input token written to the prompt cache")
    # None for a model that bills one cache-write rate whatever the TTL, which is every model outside
    # Anthropic's and some of Anthropic's own.
    cache_creation_input_token_cost_above_1hr: float | None = Field(
        description="USD per input token written to the 1-hour prompt cache; None where the map states no such rate"
    )


class PriceMap(FrozenModel):
    """A price table and the record of where it came from, as one value to price a whole report from."""

    entries: Mapping[str, Any] = Field(description="Per-token rates, keyed the way the map keys a model")
    pricing_source: PricingSource = Field(description="Which map these rates were read from")


@pure
def _rate(raw_entry: Mapping[str, Any], key: str) -> float:
    """One per-token rate out of a map entry, zero where the entry states none.

    Zero is the right reading of an absent bucket: a model that bills no cache-write surcharge simply
    has no such key, and OpenAI's entries have none at all. A whole entry of zeros is a different
    claim, which `_price_entry_or_none` refuses.
    """
    value = raw_entry.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


@pure
def _optional_rate(raw_entry: Mapping[str, Any], key: str) -> float | None:
    """One per-token rate that a map entry need not state at all, as distinct from stating zero."""
    return _rate(raw_entry, key) if key in raw_entry else None


@pure
def _price_entry_or_none(litellm_key: str, raw_entry: Mapping[str, Any]) -> PriceEntry | None:
    """One map entry's rates, or None when the entry prices nothing.

    An entry whose input and output both cost zero is a placeholder the map carries for a model
    nobody filled prices in for (`zai/glm-4.7-flash` is one), and reading it as free would report a
    real bill as nothing.
    """
    input_cost = _rate(raw_entry, "input_cost_per_token")
    output_cost = _rate(raw_entry, "output_cost_per_token")
    if not input_cost and not output_cost:
        return None
    return PriceEntry(
        litellm_key=litellm_key,
        input_cost_per_token=input_cost,
        output_cost_per_token=output_cost,
        cache_read_input_token_cost=_rate(raw_entry, "cache_read_input_token_cost"),
        cache_creation_input_token_cost=_rate(raw_entry, "cache_creation_input_token_cost"),
        cache_creation_input_token_cost_above_1hr=_optional_rate(raw_entry, _ABOVE_1HR_KEY),
    )


@pure
def _lookup_keys(model_id: str, catalog_id: str) -> tuple[str, ...]:
    """Which keys of litellm's map a reported model id is looked for under, in order."""
    if not model_id:
        return ()
    if model_id.startswith(_ANTHROPIC_PREFIX):
        return (model_id, model_id[len(_ANTHROPIC_PREFIX) :])
    # A harness that tags a model with the provider serving it reports the tag minus that first
    # segment, while litellm keys the model under the gateway that bills it: pi asked for
    # `openrouter/openai/gpt-5-mini` reports `openai/gpt-5-mini`, which the map does not hold. Taking
    # the tag back is reading the trial's own record of what it asked for, not guessing a provider --
    # hence the exact-suffix match, and hence the gateway's rate rather than the vendor's direct one.
    if catalog_id and catalog_id.partition("/")[2] == model_id:
        return (model_id, catalog_id)
    return (model_id,)


@pure
def resolve_price_entry(model_id: str, price_map: PriceMap, *, catalog_id: str = "") -> PriceEntry | None:
    """The map's rates for a model id as a harness reports it, or None where nothing prices it.

    `catalog_id` is the id the trial asked the chat to run on, empty where it asked for nothing.

    Two lookups and no guessing: the id itself, then either the bare id behind an `anthropic/` prefix
    or the catalog id the reported one is a suffix of. A model neither names is reported unpriced
    rather than approximated from a similar name, so a harness that starts answering on a new model
    shows up as a missing price instead of a silently wrong cost.
    """
    for litellm_key in _lookup_keys(model_id, catalog_id):
        raw_entry = price_map.entries.get(litellm_key)
        if not isinstance(raw_entry, Mapping):
            continue
        entry = _price_entry_or_none(litellm_key, raw_entry)
        if entry is not None:
            return entry
    return None


@pure
def _cache_write_rate(entry: PriceEntry, cache_write_ttl: CacheWriteTtl) -> float:
    """What one prompt-cache write costs at the TTL it was asked for."""
    match cache_write_ttl:
        case CacheWriteTtl.FIVE_MINUTES:
            return entry.cache_creation_input_token_cost
        case CacheWriteTtl.ONE_HOUR:
            # A model whose entry states no 1-hour rate bills one rate whatever the TTL.
            return entry.cache_creation_input_token_cost_above_1hr or entry.cache_creation_input_token_cost
        case _ as unreachable:
            assert_never(unreachable)


@pure
def price_tokens(
    entry: PriceEntry,
    tokens: TokenSnapshot,
    *,
    is_fast_mode: bool,
    cache_write_ttl: CacheWriteTtl,
) -> float | None:
    """USD for these tokens at one model's rates, each bucket at its own.

    A fast-mode request on a model outside `FAST_MODE_MODELS` is unpriced rather than priced at the
    standard rate: that rate is known to be the wrong one, and silently halving a real bill is worse
    than admitting the figure is unavailable.
    """
    if is_fast_mode and entry.litellm_key not in FAST_MODE_MODELS:
        return None
    cost_usd = (
        (tokens.input or 0) * entry.input_cost_per_token
        + (tokens.output or 0) * entry.output_cost_per_token
        + (tokens.cache_read or 0) * entry.cache_read_input_token_cost
        + (tokens.cache_creation or 0) * _cache_write_rate(entry, cache_write_ttl)
    )
    return cost_usd * FAST_MODE_PRICE_MULTIPLIER if is_fast_mode else cost_usd


@pure
def cache_write_ttl_for_harness(harness: str) -> CacheWriteTtl:
    """Which prompt-cache TTL a harness's cache writes are billed at.

    An assumption about what the harness asks the API for, not an observation: Claude Code requests
    the 1-hour prompt cache, and no record a trial leaves behind carries the TTL of an individual
    write. Every other harness is priced at the base rate, which is what asking for nothing gets.
    """
    return CacheWriteTtl.ONE_HOUR if harness == CLAUDE_HARNESS else CacheWriteTtl.FIVE_MINUTES


def load_litellm_price_map() -> PriceMap:
    """litellm's own price map, together with the record of which copy of it answered.

    The only place litellm's globals are read, so a run is priced from the map as it stood when the
    report began rather than from whatever each lookup happened to find. The version alone does not
    pin the rates -- litellm refreshes its map over the network at import and only falls back to the
    copy the wheel ships -- so where it got this one is read beside it.
    """
    return PriceMap(
        entries=litellm.model_cost,
        pricing_source=PricingSource(
            source=LITELLM_SOURCE,
            version=version("litellm"),
            map_source=str(get_model_cost_map_source_info().get("source") or ""),
        ),
    )
