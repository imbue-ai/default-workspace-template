import pytest

from imbue.minds_evals.data_types import TokenSnapshot
from imbue.minds_evals.pricing import CacheWriteTtl
from imbue.minds_evals.pricing import PriceEntry
from imbue.minds_evals.pricing import cache_write_ttl_for_harness
from imbue.minds_evals.pricing import price_tokens
from imbue.minds_evals.pricing import resolve_price_entry
from imbue.minds_evals.testing import FIXTURE_PRICE_MAP

# Every rule below is measured against the fixture price map rather than litellm's own, which is
# fetched over the network and edited upstream: the rates are what this project chose, so a figure
# named here changes only when this project changes. `test_litellm_prices_every_model_a_run_reports`
# is the one test that reads the live map, and it names no amount.
_OPUS_INPUT_RATE = 5e-6
_OPUS_OUTPUT_RATE = 2.5e-5


def _priced(model_id: str) -> PriceEntry:
    entry = resolve_price_entry(model_id, FIXTURE_PRICE_MAP)
    assert entry is not None, model_id
    return entry


def test_resolve_price_entry_prices_a_claude_id_the_harness_reports_bare() -> None:
    # A price map keys claude models by their bare id, which is what the claude harness reports.
    entry = _priced("claude-opus-4-8")

    assert entry.input_cost_per_token == pytest.approx(_OPUS_INPUT_RATE)
    assert entry.output_cost_per_token == pytest.approx(_OPUS_OUTPUT_RATE)
    assert entry.cache_read_input_token_cost < entry.input_cost_per_token
    assert entry.cache_creation_input_token_cost > entry.input_cost_per_token


def test_resolve_price_entry_strips_an_anthropic_prefix() -> None:
    """litellm carries no provider-qualified claude key, so a record that names Anthropic beside the
    model has to price exactly as the bare id does -- otherwise the same model would cost money read
    from one source and nothing read from another."""
    assert _priced("anthropic/claude-haiku-4-5") == _priced("claude-haiku-4-5")


def test_resolve_price_entry_prices_a_gateway_model_under_the_tag_the_trial_asked_for() -> None:
    """A harness reports a model without the gateway that served it, and the map keys it under the
    gateway that bills it: `openrouter/openai/gpt-5-mini` is reported as `openai/gpt-5-mini`, which the
    map does not hold. The tag is the trial's own record of what it asked for, so the two are matched
    -- and the rate is the gateway's rather than the vendor's direct one."""
    assert resolve_price_entry("openai/gpt-5-mini", FIXTURE_PRICE_MAP) is None

    entry = resolve_price_entry("openai/gpt-5-mini", FIXTURE_PRICE_MAP, catalog_id="openrouter/openai/gpt-5-mini")

    assert entry is not None and entry.litellm_key == "openrouter/openai/gpt-5-mini"
    assert entry.input_cost_per_token > _priced("gpt-5-mini").input_cost_per_token


def test_resolve_price_entry_ignores_a_catalog_id_the_reported_model_did_not_come_from() -> None:
    """Matched on the exact suffix, not on a tag merely being there: a trial that switched models
    mid-conversation reports rows the arm's tag says nothing about, and pricing one of those under the
    tag would bill another model's traffic at this model's rate. A reported id the map holds itself is
    priced under that key, at the vendor's own rate rather than the gateway's."""
    assert (
        resolve_price_entry("openai/gpt-5-mini-pro", FIXTURE_PRICE_MAP, catalog_id="openrouter/openai/gpt-5-mini")
        is None
    )

    entry = resolve_price_entry("gpt-5-mini", FIXTURE_PRICE_MAP, catalog_id="openrouter/openai/gpt-5-mini")

    assert entry is not None and entry.litellm_key == "gpt-5-mini"
    assert entry.input_cost_per_token < _priced("openrouter/openai/gpt-5-mini").input_cost_per_token


def test_resolve_price_entry_refuses_to_guess_at_a_model_the_map_does_not_hold() -> None:
    # Guessing from a similar name would price a model as another's; None makes it visibly unpriced.
    assert resolve_price_entry("mystery-model-9", FIXTURE_PRICE_MAP) is None
    assert resolve_price_entry("", FIXTURE_PRICE_MAP) is None
    # The pseudo-model Claude Code files its own synthetic notices under is no model at all.
    assert resolve_price_entry("<synthetic>", FIXTURE_PRICE_MAP) is None


def test_resolve_price_entry_reads_an_all_zero_entry_as_unpriced() -> None:
    """A price map carries placeholder rows whose every rate is zero, for a model nobody filled prices
    in for. Reading one as free would report a real bill as nothing, which is the one reading that
    cannot be recovered from."""
    assert resolve_price_entry("glm-4-7-251222", FIXTURE_PRICE_MAP) is None


def test_price_tokens_prices_every_bucket_at_its_own_rate() -> None:
    entry = _priced("claude-opus-4-8")

    cost_usd = price_tokens(
        entry,
        TokenSnapshot(input=1_000_000, output=1_000_000, cache_read=1_000_000, cache_creation=1_000_000),
        is_fast_mode=False,
        cache_write_ttl=CacheWriteTtl.FIVE_MINUTES,
    )

    # $5 input + $25 output + $0.50 cache read + $6.25 cache write.
    assert cost_usd == pytest.approx(36.75)


def test_price_tokens_charges_a_one_hour_cache_write_at_the_one_hour_rate() -> None:
    """Anthropic bills a cache write by how long it lives, and the claude harness asks for the hour.
    Pricing it at the 5-minute rate understates that bucket by 37.5%."""
    entry = _priced("claude-opus-4-8")
    tokens = TokenSnapshot(cache_creation=1_000_000)

    five_minutes = price_tokens(entry, tokens, is_fast_mode=False, cache_write_ttl=CacheWriteTtl.FIVE_MINUTES)
    one_hour = price_tokens(entry, tokens, is_fast_mode=False, cache_write_ttl=CacheWriteTtl.ONE_HOUR)

    assert five_minutes == pytest.approx(6.25)
    assert one_hour == pytest.approx(10.0)


def test_price_tokens_falls_back_to_the_base_cache_write_rate_where_the_map_states_no_other() -> None:
    """A model whose entry states one cache-write rate whatever the TTL bills the longer one at that
    rate: the absent 1-hour rate is not a missing price. Measured on cache writes alone, so a reading
    that took the absent rate as zero would report the bucket as free rather than as the same price."""
    # Bedrock's Haiku, the fixture map's entry with a cache-write rate and no 1-hour one beside it.
    entry = _priced("anthropic.claude-3-5-haiku-20241022-v1:0")
    tokens = TokenSnapshot(cache_creation=1_000_000)

    one_hour = price_tokens(entry, tokens, is_fast_mode=False, cache_write_ttl=CacheWriteTtl.ONE_HOUR)

    assert one_hour == price_tokens(entry, tokens, is_fast_mode=False, cache_write_ttl=CacheWriteTtl.FIVE_MINUTES)
    assert one_hour is not None and one_hour > 0.0


def test_price_tokens_doubles_every_bucket_in_fast_mode() -> None:
    """Fast mode returns the same tokens faster for twice the price, and the premium applies across
    the buckets rather than to output alone."""
    entry = _priced("claude-opus-4-8")
    tokens = TokenSnapshot(input=1_000, output=2_000, cache_read=30_000, cache_creation=4_000)

    standard = price_tokens(entry, tokens, is_fast_mode=False, cache_write_ttl=CacheWriteTtl.FIVE_MINUTES)
    fast = price_tokens(entry, tokens, is_fast_mode=True, cache_write_ttl=CacheWriteTtl.FIVE_MINUTES)

    assert standard is not None
    assert fast == pytest.approx(2 * standard)


def test_price_tokens_refuses_a_fast_mode_figure_for_a_model_that_cannot_serve_one() -> None:
    """Haiku rejects the tier outright, so a record claiming one is not something the standard rate
    describes: halving a real bill is worse than reporting no figure."""
    entry = _priced("claude-haiku-4-5")

    assert (
        price_tokens(entry, TokenSnapshot(input=1_000), is_fast_mode=True, cache_write_ttl=CacheWriteTtl.FIVE_MINUTES)
        is None
    )


def test_price_tokens_prices_a_models_two_tiers_apart() -> None:
    """A trial that ran some of its requests fast is priced per portion: the standard tokens at the
    standard rate and the fast ones at twice it, which is what the record is kept split for."""
    entry = _priced("claude-opus-4-8")

    standard = price_tokens(
        entry, TokenSnapshot(input=1_000_000), is_fast_mode=False, cache_write_ttl=CacheWriteTtl.FIVE_MINUTES
    )
    fast = price_tokens(
        entry, TokenSnapshot(input=1_000_000), is_fast_mode=True, cache_write_ttl=CacheWriteTtl.FIVE_MINUTES
    )

    assert standard is not None and fast is not None
    # $5/MTok standard plus $10/MTok fast, not both at either rate.
    assert standard + fast == pytest.approx(15.0)


def test_cache_write_ttl_follows_the_harness() -> None:
    # Claude Code asks for the 1-hour prompt cache; every other harness gets the base rate, which is
    # what asking for nothing is billed at.
    assert cache_write_ttl_for_harness("claude") is CacheWriteTtl.ONE_HOUR
    assert cache_write_ttl_for_harness("pi-coding") is CacheWriteTtl.FIVE_MINUTES
    assert cache_write_ttl_for_harness("") is CacheWriteTtl.FIVE_MINUTES
