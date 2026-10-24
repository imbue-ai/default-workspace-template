from datetime import datetime
from types import SimpleNamespace
from typing import Any

import litellm
import pytest

from workspace_usage.litellm_usage import UsageCallback
from workspace_usage.turn import UsageTurnError, usage_turn


def _callback(sent: list[Any]) -> UsageCallback:
    return UsageCallback(
        {"origin_kind": "APP", "origin_id": "files"}, "ws-1", emit=sent.append
    )


def _kwargs(**payload: Any) -> dict[str, Any]:
    standard = {
        "id": "chatcmpl-1",
        "custom_llm_provider": "anthropic",
        "model": "claude-haiku-4-5",
        "prompt_tokens": 1200,
        "completion_tokens": 40,
        "response_cost": 0.0021,
        "api_base": "https://api.anthropic.com/v1/messages",
    }
    standard.update(payload)
    return {"standard_logging_object": standard}


def test_a_successful_call_becomes_a_model_call_with_cache_split_out() -> None:
    sent: list[Any] = []
    usage = SimpleNamespace(
        cache_read_input_tokens=1000,
        cache_creation_input_tokens=100,
        prompt_tokens_details=None,
    )

    _callback(sent).log_success_event(
        _kwargs(),
        SimpleNamespace(usage=usage),
        datetime(2026, 10, 22, 12, 0, 0),
        datetime(2026, 10, 22, 12, 0, 1),
    )

    [event] = sent
    assert event["event_id"] == "chatcmpl-1"
    assert event["model"] == "anthropic/claude-haiku-4-5"
    assert event["tokens"] == {
        "input": 100,
        "output": 40,
        "cache_read": 1000,
        "cache_write": 100,
    }
    assert event["cost_usd"] == "0.0021"
    assert event["billing_mode"] == "API_KEY"
    assert event["status"] == "OK"
    assert event["attribution"]["origin_id"] == "files"
    assert event["attribution"]["purpose"] == "APP_CALL"
    assert event["ended_at"].endswith("+00:00")


def test_a_zero_cost_for_a_call_that_used_tokens_is_reported_unknown() -> None:
    sent: list[Any] = []

    _callback(sent).log_success_event(
        _kwargs(response_cost=0.0), SimpleNamespace(usage=None), 0.0, 1.0
    )

    assert "cost_usd" not in sent[0]


def test_a_rate_limited_failure_is_reported_as_such() -> None:
    sent: list[Any] = []
    error = litellm.RateLimitError(
        message="slow down", llm_provider="anthropic", model="claude-haiku-4-5"
    )
    kwargs = _kwargs(prompt_tokens=None, completion_tokens=None, response_cost=None)
    kwargs["exception"] = error

    _callback(sent).log_failure_event(kwargs, None, 0.0, 1.0)

    assert sent[0]["status"] == "RATE_LIMITED"
    assert sent[0]["tokens"] == {}


def test_a_call_through_a_proxy_has_an_unknown_billing_mode() -> None:
    sent: list[Any] = []

    _callback(sent).log_success_event(
        _kwargs(api_base="https://litellm.example.internal"),
        SimpleNamespace(usage=None),
        0.0,
        1.0,
    )

    assert sent[0]["billing_mode"] == "UNKNOWN"


def test_a_call_outside_any_turn_has_no_turn_id() -> None:
    sent: list[Any] = []

    _callback(sent).log_success_event(_kwargs(), SimpleNamespace(usage=None), 0.0, 1.0)

    assert "turn_id" not in sent[0]["attribution"]


def test_a_call_made_inside_a_usage_turn_carries_its_id() -> None:
    sent: list[Any] = []

    with usage_turn("request-7"):
        _callback(sent).log_success_event(
            _kwargs(), SimpleNamespace(usage=None), 0.0, 1.0
        )

    assert sent[0]["attribution"]["turn_id"] == "request-7"


def test_the_turn_read_before_the_request_holds_when_the_call_is_reported_elsewhere() -> (
    None
):
    """LiteLLM may report a call from a thread of its own, outside the caller's turn."""
    sent: list[Any] = []
    callback = _callback(sent)
    kwargs = _kwargs()

    with usage_turn("request-7"):
        callback.log_pre_api_call("claude-haiku-4-5", [], kwargs)
    callback.log_success_event(kwargs, SimpleNamespace(usage=None), 0.0, 1.0)

    assert sent[0]["attribution"]["turn_id"] == "request-7"


def test_a_calls_own_metadata_turn_id_wins_over_the_current_turn() -> None:
    sent: list[Any] = []
    kwargs = _kwargs()
    kwargs["litellm_params"] = {"metadata": {"imbue_turn_id": "job-3"}}

    with usage_turn("request-7"):
        _callback(sent).log_success_event(kwargs, SimpleNamespace(usage=None), 0.0, 1.0)

    assert sent[0]["attribution"]["turn_id"] == "job-3"


def test_a_failed_call_carries_its_turn_id_too() -> None:
    sent: list[Any] = []
    kwargs = _kwargs(prompt_tokens=None, completion_tokens=None, response_cost=None)

    with usage_turn("request-7"):
        _callback(sent).log_failure_event(kwargs, None, 0.0, 1.0)

    assert sent[0]["attribution"]["turn_id"] == "request-7"


def test_turns_nest_and_restore_the_outer_one() -> None:
    sent: list[Any] = []
    callback = _callback(sent)

    with usage_turn("outer"):
        with usage_turn("inner"):
            callback.log_success_event(_kwargs(), SimpleNamespace(usage=None), 0.0, 1.0)
        callback.log_success_event(_kwargs(), SimpleNamespace(usage=None), 0.0, 1.0)

    assert [event["attribution"]["turn_id"] for event in sent] == ["inner", "outer"]


def test_an_empty_turn_id_is_refused() -> None:
    with pytest.raises(UsageTurnError):
        with usage_turn("  "):
            pass
