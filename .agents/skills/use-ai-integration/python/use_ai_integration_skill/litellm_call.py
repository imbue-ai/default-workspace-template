from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from use_ai_integration_skill.claude_p import Usage
from use_ai_integration_skill.data_types import AIProvider, LiteLLMReply, LiteLLMRequest
from use_ai_integration_skill.errors import ProviderCallError, ProviderTimeoutError

LiteLLMCaller = Callable[[LiteLLMRequest], LiteLLMReply]


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: str | None = None


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: _Message


class _PromptTokensDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cached_tokens: int | None = None
    cache_creation_tokens: int | None = None


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens_details: _PromptTokensDetails | None = None


class _Response(BaseModel):
    """A litellm chat completion response, as far as the answer and its usage need it."""

    model_config = ConfigDict(extra="ignore")

    choices: list[_Choice] = Field(default_factory=list)
    usage: _Usage


def litellm_model_name(provider: AIProvider, model: str) -> str:
    """``model`` with its provider prefix, e.g. ``anthropic/claude-haiku-4-5``."""
    prefix = f"{provider.value}/"
    return model if model.startswith(prefix) else f"{prefix}{model}"


def call_litellm(request: LiteLLMRequest) -> LiteLLMReply:
    # imported here: litellm is large and fetches its price map when imported
    import litellm

    return call_litellm_with(
        request,
        completion_function=litellm.completion,
        cost_function=litellm.completion_cost,
    )


def _cost_or_none(
    cost_function: Callable[..., float], response: object
) -> float | None:
    try:
        return float(cost_function(completion_response=response))
    # litellm raises a bare Exception for a model missing from its price map
    except Exception:
        return None


def call_litellm_with(
    request: LiteLLMRequest,
    *,
    completion_function: Callable[..., object],
    cost_function: Callable[..., float],
) -> LiteLLMReply:
    """One completion through ``completion_function``, priced by ``cost_function``."""
    # imported here: openai is large, and only an API-key call needs it
    from openai import APITimeoutError, OpenAIError

    try:
        response = completion_function(
            model=request.model,
            messages=[
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
            api_key=request.api_key.get_secret_value(),
            api_base=request.api_base,
            timeout=request.timeout_seconds,
        )
    except APITimeoutError as e:
        raise ProviderTimeoutError(
            f"{request.model} did not answer within {request.timeout_seconds:g}s"
        ) from e
    except OpenAIError as e:
        raise ProviderCallError(f"{request.model} call failed: {e}") from e
    return litellm_reply_from_response(
        response, cost_usd=_cost_or_none(cost_function, response)
    )


def _response_as_dict(response: object) -> dict[str, object]:
    if isinstance(response, BaseModel):
        return response.model_dump()
    elif isinstance(response, Mapping):
        return dict(response)
    else:
        return {}


def litellm_reply_from_response(
    response: object, *, cost_usd: float | None
) -> LiteLLMReply:
    """The answer, usage and cost of a litellm response; input_tokens excludes cache reads and writes."""
    raw = _response_as_dict(response)
    try:
        parsed = _Response.model_validate(raw)
    except ValidationError as e:
        raise ProviderCallError(
            f"litellm returned a response without the expected shape: {e}"
        ) from e
    content = parsed.choices[0].message.content if parsed.choices else None
    if content is None or not content.strip():
        raise ProviderCallError("litellm returned no text")
    details = parsed.usage.prompt_tokens_details or _PromptTokensDetails()
    cache_read = details.cached_tokens or 0
    cache_write = details.cache_creation_tokens or 0
    usage = Usage(
        input_tokens=max(parsed.usage.prompt_tokens - cache_read - cache_write, 0),
        output_tokens=parsed.usage.completion_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )
    return LiteLLMReply(text=content, usage=usage, cost_usd=cost_usd, raw=raw)
