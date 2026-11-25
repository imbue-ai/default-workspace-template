from collections.abc import Callable

import httpx
import openai
import pytest
from pydantic import SecretStr

from use_ai_integration_skill.claude_p import Usage
from use_ai_integration_skill.data_types import AIProvider, LiteLLMRequest
from use_ai_integration_skill.errors import ProviderCallError, ProviderTimeoutError
from use_ai_integration_skill.litellm_call import (
    call_litellm_with,
    litellm_model_name,
    litellm_reply_from_response,
)

_REQUEST = LiteLLMRequest(
    model="anthropic/claude-haiku-4-5",
    system="You are a classifier.",
    prompt="Classify: invoice overdue",
    api_key=SecretStr("sk-ant-test-key"),
    api_base="https://proxy.example.invalid",
    timeout_seconds=12.5,
)


def _response(
    content: str | None = "billing",
    *,
    prompt_tokens: int = 150,
    completion_tokens: int = 7,
    cached_tokens: int | None = 30,
    cache_creation_tokens: int | None = 20,
) -> dict[str, object]:
    """A response shaped like litellm's ``ModelResponse.model_dump()``."""
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {
                "cached_tokens": cached_tokens,
                "cache_creation_tokens": cache_creation_tokens,
            },
        },
    }


def _recording_completion(
    calls: list[dict[str, object]], response: dict[str, object]
) -> Callable[..., dict[str, object]]:
    def completion(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return response

    return completion


def _raising_completion(error: Exception) -> Callable[..., dict[str, object]]:
    def completion(**kwargs: object) -> dict[str, object]:
        raise error

    return completion


def _fixed_cost(**kwargs: object) -> float:
    return 0.0021


def _unpriced_cost(**kwargs: object) -> float:
    raise Exception("This model isn't mapped yet")


def test_model_name_is_prefixed_with_the_provider_once() -> None:
    assert (
        litellm_model_name(AIProvider.ANTHROPIC, "claude-haiku-4-5")
        == "anthropic/claude-haiku-4-5"
    )
    assert litellm_model_name(AIProvider.OPENAI, "openai/gpt-x") == "openai/gpt-x"


def test_call_passes_the_model_key_base_and_messages() -> None:
    calls: list[dict[str, object]] = []

    call_litellm_with(
        _REQUEST,
        completion_function=_recording_completion(calls, _response()),
        cost_function=_fixed_cost,
    )

    assert calls == [
        {
            "model": "anthropic/claude-haiku-4-5",
            "messages": [
                {"role": "system", "content": "You are a classifier."},
                {"role": "user", "content": "Classify: invoice overdue"},
            ],
            "api_key": "sk-ant-test-key",
            "api_base": "https://proxy.example.invalid",
            "timeout": 12.5,
        }
    ]


def test_call_reports_litellms_cost() -> None:
    reply = call_litellm_with(
        _REQUEST,
        completion_function=_recording_completion([], _response()),
        cost_function=_fixed_cost,
    )

    assert reply.text == "billing"
    assert reply.cost_usd == 0.0021


def test_call_reports_no_cost_for_an_unpriced_model() -> None:
    reply = call_litellm_with(
        _REQUEST,
        completion_function=_recording_completion([], _response()),
        cost_function=_unpriced_cost,
    )

    assert reply.text == "billing"
    assert reply.cost_usd is None


def test_call_raises_provider_timeout_on_a_litellm_timeout() -> None:
    timeout = openai.APITimeoutError(
        request=httpx.Request("POST", "https://api.example.invalid")
    )

    with pytest.raises(ProviderTimeoutError, match="12.5s"):
        call_litellm_with(
            _REQUEST,
            completion_function=_raising_completion(timeout),
            cost_function=_fixed_cost,
        )


def test_call_raises_provider_call_error_on_an_api_error() -> None:
    with pytest.raises(ProviderCallError, match="bad key"):
        call_litellm_with(
            _REQUEST,
            completion_function=_raising_completion(openai.OpenAIError("bad key")),
            cost_function=_fixed_cost,
        )


def test_reply_splits_anthropic_cached_tokens_out_of_the_input() -> None:
    reply = litellm_reply_from_response(_response(), cost_usd=None)

    assert reply.usage == Usage(
        input_tokens=100, output_tokens=7, cache_read_tokens=30, cache_write_tokens=20
    )


def test_reply_counts_openai_cached_input() -> None:
    response = _response(
        prompt_tokens=2000,
        completion_tokens=40,
        cached_tokens=1500,
        cache_creation_tokens=None,
    )

    reply = litellm_reply_from_response(response, cost_usd=None)

    assert reply.usage == Usage(
        input_tokens=500, output_tokens=40, cache_read_tokens=1500, cache_write_tokens=0
    )


def test_reply_keeps_the_response_as_raw() -> None:
    response = _response()

    assert litellm_reply_from_response(response, cost_usd=None).raw == response


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(_response(content=None), id="no-content"),
        pytest.param({**_response(), "choices": []}, id="no-choices"),
    ],
)
def test_reply_without_text_raises_provider_call_error(
    response: dict[str, object],
) -> None:
    with pytest.raises(ProviderCallError, match="no text"):
        litellm_reply_from_response(response, cost_usd=None)
