import pytest

from imbue.minds_evals.data_types import DeciderResult
from imbue.minds_evals.usage import canonical_model_key
from imbue.minds_evals.usage import parse_proxy_usage_log
from imbue.minds_evals.usage import summarize_decider_usage
from imbue.minds_evals.usage import summarize_proxy_usage
from imbue.minds_evals.usage import summarize_workspace_usage
from imbue.minds_evals.usage import workspace_usage_metadata
from imbue.mngr_usage.data_types import TokenSnapshot


def _assistant_event(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> dict:
    return {
        "type": "assistant_message",
        "text": "reply",
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
        },
    }


def test_canonical_model_key_resolves_bare_claude_ids_to_anthropic() -> None:
    assert canonical_model_key("claude-opus-4-8") == "anthropic/claude-opus-4-8"


def test_canonical_model_key_passes_through_an_explicit_provider() -> None:
    assert canonical_model_key("openai/gpt-5.2") == "openai/gpt-5.2"


def test_canonical_model_key_refuses_to_guess_an_unknown_provider() -> None:
    # Guessing here would price a model as some other provider's; None makes it visibly unpriced.
    assert canonical_model_key("some-new-model") is None
    assert canonical_model_key("") is None


def test_summarize_workspace_usage_keeps_cache_buckets_separate() -> None:
    events = [
        _assistant_event("claude-opus-4-8", input_tokens=10, output_tokens=100, cache_read_tokens=5_000),
        _assistant_event("claude-opus-4-8", input_tokens=3, output_tokens=50, cache_write_tokens=2_000),
    ]

    usage = summarize_workspace_usage(events)

    assert usage.message_count == 2
    assert usage.tokens.input == 13
    assert usage.tokens.output == 150
    assert usage.tokens.cache_read == 5_000
    assert usage.tokens.cache_creation == 2_000


def test_summarize_workspace_usage_prices_from_the_shared_table() -> None:
    # Opus: input $5/M, output $25/M, cache read $0.50/M, cache write $6.25/M.
    events = [
        _assistant_event(
            "claude-opus-4-8",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_write_tokens=1_000_000,
        )
    ]

    usage = summarize_workspace_usage(events)

    assert usage.cost_usd == 5.0 + 25.0 + 0.5 + 6.25


def test_summarize_workspace_usage_reports_harbor_fields_with_cache_inclusive_input() -> None:
    events = [
        _assistant_event(
            "claude-opus-4-8", input_tokens=10, output_tokens=100, cache_read_tokens=700, cache_write_tokens=200
        )
    ]

    usage = summarize_workspace_usage(events)

    # Harbor documents n_input_tokens as including cache, so every input token counts once.
    assert usage.n_input_tokens == 910
    # Cache tokens are the ones served from cache, keeping the ratio a usable hit rate.
    assert usage.n_cache_tokens == 700
    assert usage.tokens.output == 100


def test_summarize_workspace_usage_groups_each_model_separately() -> None:
    events = [
        _assistant_event("claude-opus-4-8", input_tokens=10, output_tokens=100),
        _assistant_event("claude-haiku-4-5", input_tokens=20, output_tokens=200),
        _assistant_event("claude-opus-4-8", input_tokens=5, output_tokens=50),
    ]

    usage = summarize_workspace_usage(events)

    by_model = {entry.model: entry for entry in usage.per_model}
    opus_cost = by_model["claude-opus-4-8"].cost_usd
    haiku_cost = by_model["claude-haiku-4-5"].cost_usd
    assert opus_cost is not None and haiku_cost is not None
    assert by_model["claude-opus-4-8"].message_count == 2
    assert by_model["claude-opus-4-8"].tokens.output == 150
    assert by_model["claude-haiku-4-5"].message_count == 1
    # Haiku is an order of magnitude cheaper, so the same tokens must not be priced identically.
    assert opus_cost != haiku_cost
    assert usage.cost_usd == opus_cost + haiku_cost


def test_summarize_workspace_usage_ignores_events_that_carry_no_usage() -> None:
    events = [
        {"type": "user_message", "content": "hi"},
        {"type": "assistant_message", "text": "no usage block"},
        {"type": "assistant_message", "text": "empty usage", "model": "claude-opus-4-8", "usage": {}},
        _assistant_event("claude-opus-4-8", input_tokens=7, output_tokens=70),
    ]

    usage = summarize_workspace_usage(events)

    assert usage.message_count == 1
    assert usage.tokens.input == 7


def test_summarize_workspace_usage_leaves_cost_unknown_when_a_model_is_unpriced() -> None:
    events = [
        _assistant_event("claude-opus-4-8", input_tokens=10, output_tokens=100),
        _assistant_event("mystery-model-9", input_tokens=10, output_tokens=100),
    ]

    usage = summarize_workspace_usage(events)

    assert usage.unpriced_models == ("mystery-model-9",)
    # A partial total would look complete while understating the real spend.
    assert usage.cost_usd is None
    # The priced model still reports its own cost, so nothing is lost.
    priced = next(entry for entry in usage.per_model if entry.model == "claude-opus-4-8")
    assert priced.cost_usd is not None


def test_summarize_workspace_usage_without_any_usage_reports_unknown_not_zero() -> None:
    usage = summarize_workspace_usage([{"type": "user_message", "content": "hi"}])

    assert usage.message_count == 0
    assert usage.per_model == ()
    # A trial we have no usage data for did not cost nothing.
    assert usage.cost_usd is None


def test_workspace_usage_metadata_exposes_the_four_way_split() -> None:
    events = [
        _assistant_event("claude-opus-4-8", input_tokens=1, output_tokens=2, cache_read_tokens=3, cache_write_tokens=4)
    ]

    metadata = workspace_usage_metadata(summarize_workspace_usage(events))

    assert metadata["tokens"] == {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4}
    assert metadata["per_model"][0]["model"] == "claude-opus-4-8"
    assert metadata["unpriced_models"] == []


def test_summarize_decider_usage_totals_calls_and_counts_fallbacks() -> None:
    results = [
        DeciderResult(
            message="Sounds good.",
            model="claude-opus-4-8",
            input_token_count=100,
            output_token_count=10,
            is_fallback=False,
        ),
        DeciderResult(message="Sounds good.", model="", input_token_count=0, output_token_count=0, is_fallback=True),
    ]

    usage = summarize_decider_usage(results, "claude-opus-4-8")

    assert usage.call_count == 2
    assert usage.fallback_count == 1
    assert usage.input_token_count == 100
    assert usage.output_token_count == 10
    assert usage.cost_usd == 100 * 5e-6 + 10 * 25e-6


def test_summarize_decider_usage_with_no_calls_is_empty_but_priced_at_zero() -> None:
    # Unlike the workspace agent, a decider that made no calls really did spend nothing: the
    # literal-turn case has no LLM call to be uncertain about.
    usage = summarize_decider_usage([], "claude-opus-4-8")

    assert usage.call_count == 0
    assert usage.cost_usd == 0.0


def test_summarize_workspace_usage_flags_a_trial_that_delegated_to_a_subagent() -> None:
    events = [
        {
            **_assistant_event("claude-opus-4-8", input_tokens=10, output_tokens=100),
            "tool_calls": [{"tool_name": "Agent", "input_preview": '{"description":"build it"}'}],
        }
    ]

    usage = summarize_workspace_usage(events)

    assert usage.delegated_call_count == 1
    # The subagent's own turns are served on a separate stream, so this total excludes them.
    assert usage.is_cost_complete is False


def test_summarize_workspace_usage_recognizes_the_older_task_tool_name() -> None:
    events = [
        {
            **_assistant_event("claude-opus-4-8"),
            "tool_calls": [{"tool_name": "Task", "input_preview": "{}"}],
        }
    ]

    assert summarize_workspace_usage(events).delegated_call_count == 1


def test_summarize_workspace_usage_flags_a_worker_agent_launch() -> None:
    # The launch-task route produces no Agent tool call at all -- it is an ordinary Bash command.
    events = [
        {
            **_assistant_event("claude-opus-4-8"),
            "tool_calls": [
                {"tool_name": "Bash", "input_preview": '{"command":"uv run create_worker.py launch --name x"}'}
            ],
        }
    ]

    usage = summarize_workspace_usage(events)

    assert usage.worker_launch_count == 1
    assert usage.delegated_call_count == 0
    assert usage.is_cost_complete is False


def test_summarize_workspace_usage_treats_ordinary_tool_use_as_complete() -> None:
    events = [
        {
            **_assistant_event("claude-opus-4-8", input_tokens=10, output_tokens=100),
            "tool_calls": [
                {"tool_name": "Bash", "input_preview": '{"command":"ls -la"}'},
                {"tool_name": "Skill", "input_preview": '{"skill":"do-something-new"}'},
                {"tool_name": "Write", "input_preview": '{"file_path":"/tmp/x"}'},
            ],
        }
    ]

    usage = summarize_workspace_usage(events)

    assert usage.is_cost_complete is True
    assert usage.delegated_call_count == 0
    assert usage.worker_launch_count == 0


def test_summarize_workspace_usage_counts_one_worker_launch_per_delegation() -> None:
    # The launch-task script is called twice per delegation (launch, then await). Only the launch
    # starts new work; counting the await too would report one delegation as two.
    events = [
        {
            **_assistant_event("claude-opus-4-8"),
            "tool_calls": [
                {"tool_name": "Bash", "input_preview": '{"command":"uv run create_worker.py launch --name x"}'},
                {"tool_name": "Bash", "input_preview": '{"command":"uv run create_worker.py await --name x"}'},
            ],
        }
    ]

    assert summarize_workspace_usage(events).worker_launch_count == 1


def test_summarize_workspace_usage_ignores_zero_token_synthetic_messages() -> None:
    # Claude Code reports its pre-sign-in notice as a `<synthetic>` model with an all-zero usage
    # block. It costs nothing, and must not make a priceable trial report an unknown cost.
    events = [
        _assistant_event("<synthetic>"),
        _assistant_event("claude-opus-4-8", input_tokens=10, output_tokens=100),
    ]

    usage = summarize_workspace_usage(events)

    assert usage.unpriced_models == ()
    assert usage.cost_usd is not None
    assert [entry.model for entry in usage.per_model] == ["claude-opus-4-8"]
    assert usage.message_count == 1


def test_summarize_workspace_usage_counts_one_api_response_once() -> None:
    # A response with several content blocks becomes several transcript messages, each carrying a
    # copy of the response's usage. Summing them all would bill the same tokens two or three times.
    repeated = dict(input_tokens=2, output_tokens=215, cache_read_tokens=51_354, cache_write_tokens=407)
    events = [
        _assistant_event("claude-opus-4-8", **repeated),
        _assistant_event("claude-opus-4-8", **repeated),
        _assistant_event("claude-opus-4-8", **repeated),
    ]

    usage = summarize_workspace_usage(events)

    assert usage.message_count == 1
    assert usage.tokens.output == 215
    assert usage.tokens.cache_read == 51_354


def test_summarize_workspace_usage_keeps_identical_usage_from_separate_responses() -> None:
    # Only *consecutive* repeats are one response's blocks. Two responses that happen to report the
    # same counts, with another response between them, are genuinely separate calls.
    same = dict(input_tokens=2, output_tokens=10, cache_read_tokens=100, cache_write_tokens=0)
    other = dict(input_tokens=5, output_tokens=20, cache_read_tokens=200, cache_write_tokens=0)
    events = [
        _assistant_event("claude-opus-4-8", **same),
        _assistant_event("claude-opus-4-8", **other),
        _assistant_event("claude-opus-4-8", **same),
    ]

    usage = summarize_workspace_usage(events)

    assert usage.message_count == 3
    assert usage.tokens.output == 40


def _proxy_record(model: str = "claude-opus-4-8", speed: str | None = None, **tokens: int) -> dict:
    record: dict = {
        "model": model,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        # Present-but-null is what the proxy writes for a standard-speed request, and is what makes
        # the tier count as observed.
        "speed": speed,
    }
    record.update(tokens)
    return record


def test_summarize_proxy_usage_totals_every_request() -> None:
    usage = summarize_proxy_usage(
        [
            _proxy_record(input_tokens=10, output_tokens=100, cache_write_tokens=5_000),
            _proxy_record(input_tokens=2, output_tokens=50, cache_read_tokens=5_000),
        ]
    )

    assert usage.message_count == 2
    assert usage.tokens.input == 12
    assert usage.tokens.cache_read == 5_000
    assert usage.tokens.cache_creation == 5_000
    assert usage.cost_usd is not None


def test_summarize_proxy_usage_is_always_complete() -> None:
    # Unlike the transcript, this source is the boundary every call crosses, so delegated work is
    # included rather than merely detected -- there is nothing for it to be missing.
    usage = summarize_proxy_usage([_proxy_record(input_tokens=1, output_tokens=1)])

    assert usage.is_cost_complete is True


def test_summarize_proxy_usage_attributes_fast_mode_tokens_as_a_subset() -> None:
    usage = summarize_proxy_usage(
        [
            _proxy_record(speed="fast", input_tokens=10, output_tokens=100),
            _proxy_record(input_tokens=3, output_tokens=7),
        ]
    )

    assert usage.fast_message_count == 1
    # A subset of the totals rather than an addition to them, so the two must not be summed.
    assert usage.fast_tokens.output == 100
    assert usage.tokens.output == 107


def test_summarize_proxy_usage_prices_fast_mode_at_the_fast_mode_rate() -> None:
    # Fast mode bills the same tokens at twice the standard rate, so an identical request costs
    # exactly double -- the point of recording the tier at all.
    fast = summarize_proxy_usage([_proxy_record(speed="fast", input_tokens=1_000_000, output_tokens=1_000_000)])
    standard = summarize_proxy_usage([_proxy_record(input_tokens=1_000_000, output_tokens=1_000_000)])

    assert standard.cost_usd == pytest.approx(30.0)
    assert fast.cost_usd == pytest.approx(60.0)


def test_summarize_proxy_usage_prices_each_tier_separately_within_one_model() -> None:
    usage = summarize_proxy_usage(
        [
            _proxy_record(speed="fast", input_tokens=1_000_000),
            _proxy_record(input_tokens=1_000_000),
        ]
    )

    # $10/MTok fast + $5/MTok standard, not both at either rate.
    assert usage.cost_usd == pytest.approx(15.0)
    assert usage.is_cost_rate_certain is True


def test_summarize_proxy_usage_refuses_a_standard_price_for_fast_mode_on_a_model_without_one() -> None:
    # Sonnet cannot serve fast mode, so this should not happen -- but if it ever does, the standard
    # rate is known to be the wrong one, and halving a real bill is worse than reporting nothing.
    usage = summarize_proxy_usage([_proxy_record(model="claude-sonnet-4-6", speed="fast", input_tokens=1_000)])

    assert usage.cost_usd is None


def test_summarize_proxy_usage_certifies_the_rate_when_every_request_was_standard() -> None:
    usage = summarize_proxy_usage([_proxy_record(input_tokens=10, output_tokens=100)])

    assert usage.fast_message_count == 0
    assert usage.is_cost_rate_certain is True


def test_summarize_proxy_usage_does_not_claim_standard_speed_for_a_log_predating_the_field() -> None:
    # Such a log reports no fast requests for the same reason an all-standard trial does. Treating
    # the absent key as "standard" would certify a rate nobody observed.
    legacy = _proxy_record(input_tokens=10, output_tokens=100)
    del legacy["speed"]

    usage = summarize_proxy_usage([legacy])

    assert usage.is_speed_observed is False
    assert usage.is_cost_rate_certain is False


def test_summarize_proxy_usage_splits_fast_usage_per_model() -> None:
    # An A/B across models needs the split per arm: only some models offer fast mode at all.
    usage = summarize_proxy_usage(
        [
            _proxy_record(model="claude-opus-4-8", speed="fast", output_tokens=100),
            _proxy_record(model="claude-haiku-4-5", output_tokens=40),
        ]
    )

    by_model = {entry.model: entry for entry in usage.per_model}
    assert by_model["claude-opus-4-8"].fast_message_count == 1
    assert by_model["claude-opus-4-8"].fast_tokens.output == 100
    assert by_model["claude-haiku-4-5"].fast_message_count == 0
    assert by_model["claude-haiku-4-5"].fast_tokens == TokenSnapshot()


def test_transcript_usage_never_claims_to_have_observed_the_speed_tier() -> None:
    # The event stream carries token counts but no speed, so a transcript-sourced total can only
    # report the tier as unknown -- never as standard.
    usage = summarize_workspace_usage([_assistant_event("claude-opus-4-8", input_tokens=10, output_tokens=5)])

    assert usage.is_speed_observed is False
    assert usage.fast_message_count == 0
    assert usage.is_cost_rate_certain is False


def test_workspace_usage_metadata_reports_the_speed_tier() -> None:
    usage = summarize_proxy_usage([_proxy_record(speed="fast", input_tokens=10, output_tokens=100)])

    metadata = workspace_usage_metadata(usage)

    assert metadata["is_speed_observed"] is True
    assert metadata["is_cost_rate_certain"] is True
    assert metadata["fast_message_count"] == 1
    assert metadata["fast_tokens"]["output"] == 100
    assert metadata["per_model"][0]["fast_message_count"] == 1


def test_parse_proxy_usage_log_skips_blank_lines() -> None:
    contents = '{"model": "claude-opus-4-8", "input_tokens": 1, "output_tokens": 2, "cache_read_tokens": 0, "cache_write_tokens": 0}\n\n'

    records = parse_proxy_usage_log(contents)

    assert len(records) == 1
    assert records[0]["input_tokens"] == 1
