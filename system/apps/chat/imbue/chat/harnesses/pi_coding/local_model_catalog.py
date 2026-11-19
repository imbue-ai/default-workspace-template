"""What a model server offers a chat: its models, one row each, with the context sizes each can run at.

Ollama loads a model with the context length the model itself names
(:data:`OLLAMA_CONTEXT_PARAMETER`), else with the server's own default of 4096 tokens, and its
OpenAI-compatible endpoint takes no context length with a request (measured on 0.40.16: the
parameter is ignored there both under `options` and at the top level, and the model is loaded
at the default). The one way to run a model at another
size is a copy of it that names the size, which Ollama makes in an instant and which shares the
model's weights. A server soon lists `qwen3:8b`, `qwen3-32k` and `qwen3-40k`, which are one model.

So a server's models are grouped here: a copy that differs from a model it was made from by its
context length is that model at another size, not a model of its own. A model is offered once,
with the sizes it can run at, and the copy for a size nobody made yet is made when a chat first
asks for it (:func:`ensure_context_model`).

A server that is not Ollama says nothing of this, and its models are offered as it lists them.
"""

import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from itertools import repeat
from typing import Any
from typing import Final

import httpx
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# The sizes a model is offered at, where its own limit allows them. The first is the least a chat
# here can run with.
_CONTEXT_WINDOW_LADDER: Final = (40_960, 65_536, 131_072, 262_144)

_OLLAMA_TIMEOUT_SECONDS: Final = 5.0
# Making a copy writes one small manifest; the weights are shared.
_CREATE_TIMEOUT_SECONDS: Final = 60.0

_TOOLS_CAPABILITY: Final = "tools"
_THINKING_CAPABILITY: Final = "thinking"
# Ollama's name for the context length a model is loaded with, in a model's parameters.
OLLAMA_CONTEXT_PARAMETER: Final = "num_ctx"
_NAMED_CONTEXT_PATTERN: Final = re.compile(rf"^{OLLAMA_CONTEXT_PARAMETER}\s+(\d+)\s*$", re.MULTILINE)
_DEFAULT_TAG: Final = "latest"


class LocalModelCatalogError(Exception):
    """A server would not make the copy of a model a chat asked for. The message is shown to the user."""


class LocalModel(FrozenModel):
    """One model as its server lists it, with what the server says about it."""

    # Also read from the Imbue Studio app's listing, which a newer app may say more in.
    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="The model's id on the server")
    base_id: str = Field(
        description="The model this one is a copy of at another context size; its own id when it is no such copy"
    )
    context_window: int | None = Field(
        default=None, description="The context length the model names for itself, in tokens; None when it names none"
    )
    max_context_window: int | None = Field(
        default=None,
        description="The longest context the model can take, in tokens; None when the server does not say",
    )
    parameter_size: str = Field(
        default="", description="The model's size as the server words it ('8.2B'); '' if unsaid"
    )
    is_tool_caller: bool | None = Field(
        default=None, description="Whether the server lists the model as calling tools; None when it does not say"
    )
    is_reasoning: bool | None = Field(
        default=None, description="Whether the server lists the model as reasoning; None when it does not say"
    )


class LocalModelChoice(FrozenModel):
    """One model a chat can be put on, with the context sizes it can run at there."""

    model_id: str = Field(description="The model's id on the server, whichever of its copies ends up running")
    detail: str = Field(default="", description="What tells this model from its neighbours, as shown beside it")
    context_windows: tuple[int, ...] = Field(
        default=(),
        description="The context sizes on offer, ascending, in tokens; empty when the server does not say what "
        "the model takes, and the size is then whatever the user names",
    )
    unusable_reason: str | None = Field(
        default=None, description="Why a chat here cannot run on the model; None when it can"
    )


@pure
def with_tag(model_id: str) -> str:
    """The id with its tag spelled out, as Ollama spells it when it names a model's parent."""
    name = model_id.rsplit("/", 1)[-1]
    return model_id if ":" in name else f"{model_id}:{_DEFAULT_TAG}"


@pure
def context_model_id(base_id: str, context_window: int) -> str:
    """The id the copy of ``base_id`` at ``context_window`` is made under."""
    return f"{with_tag(base_id)}-ctx{context_window}"


@pure
def format_context_window(context_window: int) -> str:
    """A context size as shown: 40960 is "40K", and a size that is no round count of kibitokens is spelled out."""
    if context_window % 1024 == 0:
        return f"{context_window // 1024}K"
    return f"{context_window:,}"


@pure
def offer_context_windows(
    max_context_window: int | None, existing: Sequence[int], min_context_window: int
) -> tuple[int, ...]:
    """The sizes a model is offered at: the usual ones and its own limit, plus the sizes it already has a copy at.

    Nothing under ``min_context_window`` is offered, and nothing over the model's limit: Ollama
    loads a model named a longer context than it takes at its limit.
    """
    ceiling = max_context_window if max_context_window is not None else _CONTEXT_WINDOW_LADDER[-1]
    candidates = {*_CONTEXT_WINDOW_LADDER, *existing}
    if max_context_window is not None:
        candidates.add(max_context_window)
    return tuple(sorted(size for size in candidates if min_context_window <= size <= ceiling))


@pure
def _unusable_reason(model: LocalModel, min_context_window: int) -> str | None:
    if model.is_tool_caller is False:
        return "It does not call tools, and chats here work by calling tools."
    if model.max_context_window is not None and model.max_context_window < min_context_window:
        return (
            f"It takes a context of {model.max_context_window:,} tokens at most, and a chat here needs"
            f" {min_context_window:,}."
        )
    return None


@pure
def find_context_models(models: Sequence[LocalModel], base_id: str) -> dict[int, str]:
    """The copies of ``base_id`` the server has, by the context length each names: the first by id for a length."""
    copies: dict[int, str] = {}
    for model in sorted(models, key=lambda each: each.id):
        if model.base_id == base_id and model.context_window is not None:
            copies.setdefault(model.context_window, model.id)
    return copies


@pure
def group_model_choices(models: Sequence[LocalModel], min_context_window: int) -> tuple[LocalModelChoice, ...]:
    """One choice per model, its copies at other context sizes folded into the sizes it offers.

    Models a chat can run on come first, in the server's order.
    """
    base_by_id = {model.id: model for model in models if model.base_id == model.id}
    choices: list[LocalModelChoice] = []
    for base in base_by_id.values():
        reason = _unusable_reason(base, min_context_window)
        has_known_sizes = base.max_context_window is not None or base.context_window is not None
        sizes = (
            offer_context_windows(
                base.max_context_window, tuple(find_context_models(models, base.id)), min_context_window
            )
            if reason is None and has_known_sizes
            else ()
        )
        choices.append(
            LocalModelChoice(
                model_id=base.id, detail=base.parameter_size, context_windows=sizes, unusable_reason=reason
            )
        )
    return tuple(sorted(choices, key=lambda choice: choice.unusable_reason is not None))


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _read_named_context_window(client: httpx.Client, root_url: str, api_key: str, model_id: str) -> int | None:
    """The context length ``model_id`` names for itself, which only Ollama's description of one model carries."""
    try:
        response = client.post(
            f"{root_url}/api/show",
            json={"model": model_id},
            headers=_auth_headers(api_key),
            timeout=_OLLAMA_TIMEOUT_SECONDS,
        )
        payload = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as e:
        logger.debug("Could not read what {} says of {}: {}", root_url, model_id, e)
        return None
    parameters = payload.get("parameters") if isinstance(payload, dict) else None
    match = _NAMED_CONTEXT_PATTERN.search(parameters) if isinstance(parameters, str) else None
    return int(match.group(1)) if match is not None else None


def _to_local_model(entry: Any, listed_ids: frozenset[str], named_context_window: int | None) -> LocalModel | None:
    model_id = entry.get("name") if isinstance(entry, dict) else None
    if not isinstance(model_id, str) or model_id == "":
        return None
    details = entry.get("details")
    details = details if isinstance(details, dict) else {}
    parent = details.get("parent_model")
    parent_id = with_tag(parent) if isinstance(parent, str) and parent != "" else None
    # A copy is its parent at another size only when it names a size and the parent is still there.
    is_context_copy = parent_id is not None and parent_id in listed_ids and named_context_window is not None
    max_context_window = details.get("context_length")
    parameter_size = details.get("parameter_size")
    capabilities = entry.get("capabilities")
    is_capability_listed = isinstance(capabilities, list)
    return LocalModel(
        id=model_id,
        base_id=parent_id if is_context_copy and parent_id is not None else model_id,
        context_window=named_context_window,
        max_context_window=max_context_window
        if isinstance(max_context_window, int) and max_context_window > 0
        else None,
        parameter_size=parameter_size if isinstance(parameter_size, str) else "",
        is_tool_caller=_TOOLS_CAPABILITY in capabilities if is_capability_listed else None,
        is_reasoning=_THINKING_CAPABILITY in capabilities if is_capability_listed else None,
    )


def read_ollama_models(client: httpx.Client, base_url: str, api_key: str) -> tuple[LocalModel, ...] | None:
    """The models of the server at ``base_url`` as Ollama describes them; None for a server that is not Ollama.

    Ollama answers beside its OpenAI-compatible root. Its listing names each model's parent and
    limit, and a second question per model made from another gives the context length it names.
    """
    root_url = base_url.removesuffix("/v1")
    try:
        response = client.get(f"{root_url}/api/tags", headers=_auth_headers(api_key), timeout=_OLLAMA_TIMEOUT_SECONDS)
        payload = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as e:
        logger.debug("No Ollama model listing from {}: {}", root_url, e)
        return None
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return None
    named = [entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("name"), str)]
    if not named:
        return ()
    listed_ids = frozenset(with_tag(entry["name"]) for entry in named)
    with ThreadPoolExecutor(max_workers=min(len(named), 8)) as pool:
        named_context_windows = list(
            pool.map(
                _read_named_context_window,
                repeat(client),
                repeat(root_url),
                repeat(api_key),
                [entry["name"] for entry in named],
            )
        )
    models = (
        _to_local_model(entry, listed_ids, named_context_window)
        for entry, named_context_window in zip(named, named_context_windows, strict=True)
    )
    return tuple(model for model in models if model is not None)


def create_context_model(
    client: httpx.Client, base_url: str, api_key: str, copy_id: str, base_id: str, context_window: int
) -> None:
    """Make ``copy_id`` on an Ollama server: ``base_id`` loaded with a context of ``context_window`` tokens.

    Raises LocalModelCatalogError, worded for the user, when the server will not make it.
    """
    root_url = base_url.removesuffix("/v1")
    try:
        response = client.post(
            f"{root_url}/api/create",
            json={
                "model": copy_id,
                "from": base_id,
                "parameters": {OLLAMA_CONTEXT_PARAMETER: context_window},
                "stream": False,
            },
            headers=_auth_headers(api_key),
            timeout=_CREATE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        raise LocalModelCatalogError(
            f"The model server stopped answering while setting {base_id} up for a context of {context_window:,} tokens."
        ) from e
    if response.status_code != 200:
        logger.debug("Ollama answered {} to creating {}: {}", response.status_code, copy_id, response.text[:200])
        raise LocalModelCatalogError(
            f"The model server would not set {base_id} up for a context of {context_window:,} tokens"
            f" (it answered {response.status_code})."
        )


def ensure_context_model(
    client: httpx.Client, base_url: str, api_key: str, models: Sequence[LocalModel], base_id: str, context_window: int
) -> str:
    """The id of ``base_id``'s copy at ``context_window`` on an Ollama server, made now when it has none."""
    copy_id = find_context_models(models, base_id).get(context_window, context_model_id(base_id, context_window))
    if not any(model.id == copy_id for model in models):
        create_context_model(client, base_url, api_key, copy_id, base_id, context_window)
    return copy_id
