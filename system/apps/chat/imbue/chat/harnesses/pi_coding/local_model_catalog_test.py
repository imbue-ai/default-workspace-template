"""Tests for a model server's catalog: one row per model, with the context sizes each runs at.

The server is an `httpx.MockTransport` answering as Ollama 0.40.16 was measured to: a listing
that names each model's parent, limit and capabilities, and a description per model whose
parameters carry the context length a copy names.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from imbue.chat.harnesses.pi_coding.local_model_catalog import LocalModel
from imbue.chat.harnesses.pi_coding.local_model_catalog import LocalModelCatalogError
from imbue.chat.harnesses.pi_coding.local_model_catalog import OLLAMA_CONTEXT_PARAMETER
from imbue.chat.harnesses.pi_coding.local_model_catalog import context_model_id
from imbue.chat.harnesses.pi_coding.local_model_catalog import ensure_context_model
from imbue.chat.harnesses.pi_coding.local_model_catalog import find_context_models
from imbue.chat.harnesses.pi_coding.local_model_catalog import format_context_window
from imbue.chat.harnesses.pi_coding.local_model_catalog import group_model_choices
from imbue.chat.harnesses.pi_coding.local_model_catalog import offer_context_windows
from imbue.chat.harnesses.pi_coding.local_model_catalog import read_ollama_models

_OLLAMA = "http://127.0.0.1:11434/v1"
_MIN = 40_960


def _tag(
    name: str, parent: str = "", context_length: int = 40_960, capabilities: tuple[str, ...] = ("completion", "tools")
) -> dict[str, Any]:
    return {
        "name": name,
        "details": {"parent_model": parent, "parameter_size": "8.2B", "context_length": context_length},
        "capabilities": list(capabilities),
    }


def _ollama(
    tags: list[dict[str, Any]], named_context_by_model: dict[str, int], created: list[dict[str, Any]] | None = None
) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": tags})
        if request.url.path == "/api/show":
            model = json.loads(request.content)["model"]
            named_context = named_context_by_model.get(model)
            parameters = "temperature                    0.6\n" + (
                f"{OLLAMA_CONTEXT_PARAMETER}                        {named_context}\n"
                if named_context is not None
                else ""
            )
            return httpx.Response(200, json={"parameters": parameters + 'stop                           "<|im_end|>"'})
        if request.url.path == "/api/create" and created is not None:
            created.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handle))


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _base(model_id: str, max_context_window: int | None = 131_072, is_tool_caller: bool | None = True) -> LocalModel:
    return LocalModel(
        id=model_id, base_id=model_id, max_context_window=max_context_window, is_tool_caller=is_tool_caller
    )


def _copy(model_id: str, base_id: str, context_window: int) -> LocalModel:
    return LocalModel(id=model_id, base_id=base_id, context_window=context_window, max_context_window=131_072)


def test_a_copy_that_names_a_context_length_is_its_parent_at_another_size() -> None:
    client = _ollama(
        [_tag("qwen3-40k:latest", parent="qwen3:8b"), _tag("qwen3-32k:latest", parent="qwen3:8b"), _tag("qwen3:8b")],
        {"qwen3-40k:latest": 40_960, "qwen3-32k:latest": 32_768},
    )

    models = read_ollama_models(client, _OLLAMA, "")

    assert models is not None
    assert [(model.id, model.base_id, model.context_window) for model in models] == [
        ("qwen3-40k:latest", "qwen3:8b", 40_960),
        ("qwen3-32k:latest", "qwen3:8b", 32_768),
        ("qwen3:8b", "qwen3:8b", None),
    ]
    assert models[2].max_context_window == 40_960
    assert models[2].parameter_size == "8.2B"


def test_a_parent_named_without_its_tag_is_still_found() -> None:
    client = _ollama(
        [_tag("llama3:latest"), _tag("llama3-big:latest", parent="llama3")], {"llama3-big:latest": 65_536}
    )

    models = read_ollama_models(client, _OLLAMA, "")

    assert models is not None
    assert models[1].base_id == "llama3:latest"


@pytest.mark.parametrize(
    ("tags", "named_context_by_model"),
    [
        # Made from another model, but naming no context length: a model of its own.
        ([_tag("qwen3:8b"), _tag("pirate:latest", parent="qwen3:8b")], {}),
        # Naming one, but the model it was made from is gone.
        ([_tag("orphan:latest", parent="qwen3:8b")], {"orphan:latest": 65_536}),
    ],
)
def test_a_copy_that_is_not_a_size_of_a_listed_model_stands_for_itself(
    tags: list[dict[str, Any]], named_context_by_model: dict[str, int]
) -> None:
    models = read_ollama_models(_ollama(tags, named_context_by_model), _OLLAMA, "")

    assert models is not None
    assert models[-1].base_id == models[-1].id


def test_what_the_server_lists_of_a_model_s_abilities_is_kept() -> None:
    client = _ollama(
        [
            _tag("qwen3:8b", capabilities=("completion", "tools", "thinking")),
            _tag("gemma3:4b", capabilities=("completion",)),
        ],
        {},
    )

    models = read_ollama_models(client, _OLLAMA, "")

    assert models is not None
    assert [(model.is_tool_caller, model.is_reasoning) for model in models] == [(True, True), (False, False)]


def test_a_server_that_is_not_ollama_describes_nothing() -> None:
    assert read_ollama_models(_client(lambda _request: httpx.Response(404)), "http://127.0.0.1:1234/v1", "") is None


def test_a_server_that_does_not_answer_describes_nothing() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert read_ollama_models(_client(refuse), _OLLAMA, "") is None


def test_an_ollama_with_no_models_describes_none() -> None:
    assert read_ollama_models(_ollama([], {}), _OLLAMA, "") == ()


def test_a_model_is_offered_once_with_the_sizes_of_its_copies_folded_in() -> None:
    models = (_base("llama3.3:latest"), _copy("llama3.3-50k:latest", "llama3.3:latest", 50_000))

    (choice,) = group_model_choices(models, _MIN)

    assert choice.model_id == "llama3.3:latest"
    assert choice.context_windows == (40_960, 50_000, 65_536, 131_072)
    assert choice.unusable_reason is None


def test_a_model_whose_limit_is_under_what_a_chat_needs_says_so_and_comes_last() -> None:
    choices = group_model_choices((_base("qwen2.5:7b", max_context_window=32_768), _base("qwen3:8b", 40_960)), _MIN)

    assert [choice.model_id for choice in choices] == ["qwen3:8b", "qwen2.5:7b"]
    assert choices[0].context_windows == (40_960,)
    assert choices[1].context_windows == ()
    assert choices[1].unusable_reason == ("It takes a context of 32,768 tokens at most, and a chat here needs 40,960.")


def test_a_model_that_does_not_call_tools_cannot_run_a_chat() -> None:
    (choice,) = group_model_choices((_base("gemma3:4b", is_tool_caller=False),), _MIN)

    assert choice.unusable_reason == "It does not call tools, and chats here work by calling tools."


def test_a_model_the_server_says_nothing_of_offers_no_sizes() -> None:
    (choice,) = group_model_choices((LocalModel(id="some-model", base_id="some-model"),), _MIN)

    assert (choice.context_windows, choice.unusable_reason) == ((), None)


@pytest.mark.parametrize(
    ("max_context_window", "existing", "expected"),
    [
        (40_960, (), (40_960,)),
        (131_072, (), (40_960, 65_536, 131_072)),
        # A limit that is none of the usual sizes is offered as itself, and nothing above it.
        (128_000, (), (40_960, 65_536, 128_000)),
        # A copy below what a chat needs is not a size on offer.
        (131_072, (32_768, 50_000), (40_960, 50_000, 65_536, 131_072)),
        (32_768, (32_768,), ()),
        (None, (), (40_960, 65_536, 131_072, 262_144)),
    ],
)
def test_the_sizes_on_offer_run_from_what_a_chat_needs_to_the_model_s_limit(
    max_context_window: int | None, existing: tuple[int, ...], expected: tuple[int, ...]
) -> None:
    assert offer_context_windows(max_context_window, existing, _MIN) == expected


def test_the_first_copy_by_id_stands_for_a_size_two_copies_share() -> None:
    models = (_base("m:latest"), _copy("m-b:latest", "m:latest", 65_536), _copy("m-a:latest", "m:latest", 65_536))

    assert find_context_models(models, "m:latest") == {65_536: "m-a:latest"}


@pytest.mark.parametrize(
    ("context_window", "shown"), [(40_960, "40K"), (65_536, "64K"), (131_072, "128K"), (50_000, "50,000")]
)
def test_a_context_size_is_shown_in_thousands_where_it_is_a_round_count(context_window: int, shown: str) -> None:
    assert format_context_window(context_window) == shown


@pytest.mark.parametrize(
    ("base_id", "copy_id"),
    [
        ("qwen3:8b", "qwen3:8b-ctx65536"),
        ("llama3", "llama3:latest-ctx65536"),
        ("me/llama3", "me/llama3:latest-ctx65536"),
    ],
)
def test_a_copy_is_named_for_its_model_and_its_size(base_id: str, copy_id: str) -> None:
    assert context_model_id(base_id, 65_536) == copy_id


def test_a_size_with_a_copy_is_that_copy_and_nothing_is_made() -> None:
    created: list[dict[str, Any]] = []
    models = (_base("qwen3:8b"), _copy("qwen3-40k:latest", "qwen3:8b", 40_960))

    copy_id = ensure_context_model(_ollama([], {}, created), _OLLAMA, "", models, "qwen3:8b", 40_960)

    assert copy_id == "qwen3-40k:latest"
    assert created == []


def test_a_size_with_no_copy_has_one_made_from_the_model() -> None:
    created: list[dict[str, Any]] = []

    copy_id = ensure_context_model(_ollama([], {}, created), _OLLAMA, "", (_base("qwen3:8b"),), "qwen3:8b", 65_536)

    assert copy_id == "qwen3:8b-ctx65536"
    assert created == [
        {
            "model": "qwen3:8b-ctx65536",
            "from": "qwen3:8b",
            "parameters": {OLLAMA_CONTEXT_PARAMETER: 65_536},
            "stream": False,
        }
    ]


def test_a_server_that_will_not_make_the_copy_is_reported_with_the_model_and_size() -> None:
    client = _client(lambda _request: httpx.Response(500, json={"error": "no space left on device"}))

    with pytest.raises(
        LocalModelCatalogError, match=r"qwen3:8b up for a context of 65,536 tokens \(it answered 500\)"
    ):
        ensure_context_model(client, _OLLAMA, "", (_base("qwen3:8b"),), "qwen3:8b", 65_536)


def test_a_server_that_drops_out_while_making_the_copy_is_reported() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LocalModelCatalogError, match="stopped answering"):
        ensure_context_model(_client(refuse), _OLLAMA, "", (_base("qwen3:8b"),), "qwen3:8b", 65_536)
