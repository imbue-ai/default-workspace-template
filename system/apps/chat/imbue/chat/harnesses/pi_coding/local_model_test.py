"""Tests for the local model provider: finding a server, checking a model, and the file pi reads.

The server is an `httpx.MockTransport`, answering the way Ollama 0.40 was measured to: the same
request shapes, and the two answers that tell a model that calls tools from one that only says so.
"""

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from imbue.chat.harnesses.pi_coding.local_model import DEFAULT_CONTEXT_WINDOW
from imbue.chat.harnesses.pi_coding.local_model import LOCAL_MODEL_PROVIDER_ID
from imbue.chat.harnesses.pi_coding.local_model import LocalModelError
from imbue.chat.harnesses.pi_coding.local_model import LocalModelProfile
from imbue.chat.harnesses.pi_coding.local_model import MIN_CONTEXT_WINDOW
from imbue.chat.harnesses.pi_coding.local_model import check_tool_call
from imbue.chat.harnesses.pi_coding.local_model import describe_server
from imbue.chat.harnesses.pi_coding.local_model import discover_servers
from imbue.chat.harnesses.pi_coding.local_model import is_server_answering
from imbue.chat.harnesses.pi_coding.local_model import list_models
from imbue.chat.harnesses.pi_coding.local_model import normalize_base_url
from imbue.chat.harnesses.pi_coding.local_model import read_loaded_context_window
from imbue.chat.harnesses.pi_coding.local_model import read_server_base_url
from imbue.chat.harnesses.pi_coding.local_model import verify_model
from imbue.chat.harnesses.pi_coding.local_model import write_models_file

_OLLAMA = "http://host.docker.internal:11434/v1"
_STRUCTURED_CALL: dict[str, object] = {
    "role": "assistant",
    "content": "",
    "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
}
# What qwen3:8b answered: its reasoning apart from the call, in the field Ollama uses for it.
_CALL_AFTER_REASONING: dict[str, object] = {**_STRUCTURED_CALL, "reasoning": "The user wants the weather in Paris."}
# What qwen2.5-coder:7b answered: the call it was asked for, as text pi cannot run.
_CALL_AS_TEXT: dict[str, object] = {
    "role": "assistant",
    "content": '{"name": "get_weather", "arguments": {"city": "Paris"}}',
}


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ollama(models: tuple[str, ...], message: dict[str, object], loaded_context: int | None) -> httpx.Client:
    """A server on Ollama's port listing ``models``, answering a chat with ``message``, and
    reporting the first model loaded at ``loaded_context`` (or reporting nothing, as a server
    that is not Ollama does)."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host != "host.docker.internal" or request.url.port != 11434:
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": model} for model in models]})
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": message}]})
        if request.url.path == "/api/ps" and loaded_context is not None:
            return httpx.Response(200, json={"models": [{"name": models[0], "context_length": loaded_context}]})
        return httpx.Response(404)

    return _client(handle)


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("localhost:1234", "http://localhost:1234/v1"),
        ("http://localhost:11434/", "http://localhost:11434/v1"),
        ("  https://models.example.com/openai/v1/  ", "https://models.example.com/openai/v1"),
        ("http://10.0.0.5:8080/v1", "http://10.0.0.5:8080/v1"),
    ],
)
def test_an_address_is_read_the_way_a_person_types_it(typed: str, expected: str) -> None:
    assert normalize_base_url(typed) == expected


@pytest.mark.parametrize("typed", ["", "   ", "ftp://host:21", "http://"])
def test_what_is_not_a_server_address_is_refused(typed: str) -> None:
    with pytest.raises(LocalModelError):
        normalize_base_url(typed)


def test_a_server_is_described_by_its_port_and_its_models() -> None:
    server = describe_server(_ollama(("qwen2.5:7b", "llama3.1:8b"), _STRUCTURED_CALL, None), _OLLAMA, "")

    assert server.name == "Ollama"
    assert server.models == ("qwen2.5:7b", "llama3.1:8b")
    assert server.base_url == _OLLAMA


def test_a_server_on_an_unknown_port_is_named_by_its_address() -> None:
    client = _client(lambda _request: httpx.Response(200, json={"data": [{"id": "m"}]}))

    assert describe_server(client, "http://box.lan:9000/v1", "").name == "box.lan:9000"


def test_the_key_is_sent_as_a_bearer_token() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"data": []})

    list_models(_client(handle), "http://box.lan:9000/v1", "sk-secret")

    assert seen == ["Bearer sk-secret"]


@pytest.mark.parametrize(
    ("response", "fragment"),
    [
        (httpx.Response(401, json={"error": "unauthorized"}), "API key"),
        (httpx.Response(500, text="boom"), "500"),
        (httpx.Response(200, text="<html>not json</html>"), "OpenAI-compatible"),
        (httpx.Response(200, json={"models": []}), "OpenAI-compatible"),
    ],
)
def test_an_answer_that_is_not_a_model_list_says_why(response: httpx.Response, fragment: str) -> None:
    with pytest.raises(LocalModelError, match=fragment):
        list_models(_client(lambda _request: response), "http://box.lan:9000/v1", "")


def test_no_answer_at_all_names_the_address() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LocalModelError, match="http://box.lan:9000/v1"):
        list_models(_client(refuse), "http://box.lan:9000/v1", "")


def test_discovery_finds_the_server_on_the_host_that_answers() -> None:
    """A workspace in a container reaches the user's machine by Docker's name for it, not by loopback."""
    servers = discover_servers(_ollama(("qwen2.5:7b",), _STRUCTURED_CALL, None))

    assert [(server.name, server.base_url) for server in servers] == [("Ollama", _OLLAMA)]


def test_discovery_offers_a_port_once_and_prefers_the_workspace_s_own_loopback() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.port == 1234 and request.url.host in ("127.0.0.1", "host.docker.internal"):
            return httpx.Response(200, json={"data": [{"id": f"model-on-{request.url.host}"}]})
        raise httpx.ConnectError("connection refused", request=request)

    (server,) = discover_servers(_client(handle))

    assert server.name == "LM Studio"
    assert server.base_url == "http://127.0.0.1:1234/v1"


def test_discovery_with_nothing_running_finds_nothing() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert discover_servers(_client(refuse)) == ()


def test_a_structured_tool_call_passes() -> None:
    assert check_tool_call(_ollama(("qwen2.5:7b",), _STRUCTURED_CALL, None), _OLLAMA, "", "qwen2.5:7b") is False


def test_reasoning_sent_apart_from_the_answer_marks_a_reasoning_model() -> None:
    assert check_tool_call(_ollama(("qwen3:8b",), _CALL_AFTER_REASONING, None), _OLLAMA, "", "qwen3:8b") is True


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "reasoning_text"])
def test_reasoning_is_found_in_whichever_field_the_server_uses(field: str) -> None:
    message: dict[str, object] = {**_STRUCTURED_CALL, field: "thinking it over"}

    assert check_tool_call(_ollama(("m",), message, None), _OLLAMA, "", "m") is True


def test_the_check_leaves_a_reasoning_model_room_to_think_before_it_calls() -> None:
    """Reasoning counts against the token limit, so a tight one cuts the call off."""
    bodies: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": _CALL_AFTER_REASONING}]})

    check_tool_call(_client(handle), _OLLAMA, "", "qwen3:8b")

    (body,) = bodies
    assert isinstance(body["max_tokens"], int) and body["max_tokens"] >= 1000


def test_a_tool_call_printed_as_text_is_not_a_tool_call() -> None:
    """The failure that is otherwise silent: pi runs nothing and reports the turn as finished."""
    with pytest.raises(LocalModelError, match="without calling a tool"):
        check_tool_call(_ollama(("qwen2.5-coder:7b",), _CALL_AS_TEXT, None), _OLLAMA, "", "qwen2.5-coder:7b")


def test_the_check_asks_the_named_model_for_a_tool() -> None:
    bodies: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": _STRUCTURED_CALL}]})

    check_tool_call(_client(handle), _OLLAMA, "", "qwen2.5:7b")

    (body,) = bodies
    assert body["model"] == "qwen2.5:7b"
    assert body["tools"]


def test_a_server_that_will_not_run_the_model_is_quoted() -> None:
    refusal = httpx.Response(400, json={"error": {"message": "gemma3:1b does not support tools"}})

    with pytest.raises(LocalModelError, match="gemma3:1b does not support tools"):
        check_tool_call(_client(lambda _request: refusal), _OLLAMA, "", "gemma3:1b")


def test_a_model_that_never_answers_is_reported_as_slow() -> None:
    def stall(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(LocalModelError, match="took too long"):
        check_tool_call(_client(stall), _OLLAMA, "", "qwen2.5:7b")


def test_the_loaded_context_is_read_from_ollama() -> None:
    client = _ollama(("qwen2.5:7b",), _STRUCTURED_CALL, 4096)

    assert read_loaded_context_window(client, _OLLAMA, "qwen2.5:7b") == 4096
    assert read_loaded_context_window(client, _OLLAMA, "another-model") is None


def test_a_server_that_reports_no_loaded_models_gives_no_context() -> None:
    assert read_loaded_context_window(_ollama(("m",), _STRUCTURED_CALL, None), _OLLAMA, "m") is None


def test_the_window_pi_is_told_is_never_more_than_the_server_loaded() -> None:
    """Ollama keeps its own context length whatever pi is told, and truncates what does not fit."""
    client = _ollama(("qwen2.5:7b",), _STRUCTURED_CALL, 40_000)

    assert verify_model(client, _OLLAMA, "", "qwen2.5:7b", DEFAULT_CONTEXT_WINDOW) == LocalModelProfile(
        context_window=40_000, is_reasoning=False
    )


def test_the_window_asked_for_stands_when_the_server_does_not_say() -> None:
    client = _ollama(("qwen2.5:7b",), _STRUCTURED_CALL, None)

    assert verify_model(client, _OLLAMA, "", "qwen2.5:7b", 100_000).context_window == 100_000


def test_a_smaller_window_than_the_server_loaded_is_kept() -> None:
    client = _ollama(("qwen2.5:7b",), _STRUCTURED_CALL, 131_072)

    profile = verify_model(client, _OLLAMA, "", "qwen2.5:7b", DEFAULT_CONTEXT_WINDOW)

    assert profile.context_window == DEFAULT_CONTEXT_WINDOW


def test_a_reasoning_model_on_ollama_is_told_how_its_reasoning_is_turned_off() -> None:
    client = _ollama(("qwen3:8b",), _CALL_AFTER_REASONING, 40_960)

    assert verify_model(client, _OLLAMA, "", "qwen3:8b", DEFAULT_CONTEXT_WINDOW) == LocalModelProfile(
        context_window=40_960, is_reasoning=True, reasoning_off_effort="none"
    )


def test_a_reasoning_model_on_a_server_that_is_not_ollama_names_no_way_to_turn_it_off() -> None:
    """What stops the reasoning was measured on Ollama alone, so no other server is sent it."""
    client = _ollama(("qwen3:8b",), _CALL_AFTER_REASONING, None)

    assert verify_model(client, _OLLAMA, "", "qwen3:8b", DEFAULT_CONTEXT_WINDOW) == LocalModelProfile(
        context_window=DEFAULT_CONTEXT_WINDOW, is_reasoning=True
    )


def test_ollama_s_default_context_is_refused_with_the_setting_that_fixes_it() -> None:
    """4096 is what Ollama loads a model with unless told otherwise: a sixth of a new chat's first request."""
    client = _ollama(("qwen2.5:7b",), _STRUCTURED_CALL, 4096)

    with pytest.raises(LocalModelError, match="OLLAMA_CONTEXT_LENGTH"):
        verify_model(client, _OLLAMA, "", "qwen2.5:7b", DEFAULT_CONTEXT_WINDOW)


def test_a_window_too_small_to_hold_the_instructions_is_refused_before_the_server_is_asked() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("should not be asked", request=request)

    with pytest.raises(LocalModelError, match="at least"):
        verify_model(_client(unreachable), _OLLAMA, "", "qwen2.5:7b", MIN_CONTEXT_WINDOW - 1)


def test_no_model_is_refused() -> None:
    with pytest.raises(LocalModelError, match="Pick a model"):
        verify_model(_ollama(("m",), _STRUCTURED_CALL, None), _OLLAMA, "", "  ", DEFAULT_CONTEXT_WINDOW)


def test_the_models_file_is_a_pi_custom_provider_holding_no_key(tmp_path: Path) -> None:
    write_models_file(tmp_path, _OLLAMA, "qwen2.5:7b", LocalModelProfile(context_window=32_768, is_reasoning=False))

    path = tmp_path / "models.json"
    provider = json.loads(path.read_text())["providers"][LOCAL_MODEL_PROVIDER_ID]
    assert provider == {
        "baseUrl": _OLLAMA,
        "api": "openai-completions",
        "models": [{"id": "qwen2.5:7b", "contextWindow": 32_768, "maxTokens": 8_192}],
    }
    assert path.stat().st_mode & 0o077 == 0


def test_the_output_limit_leaves_room_in_a_small_window(tmp_path: Path) -> None:
    write_models_file(tmp_path, _OLLAMA, "qwen2.5:7b", LocalModelProfile(context_window=16_384, is_reasoning=False))

    (model,) = json.loads((tmp_path / "models.json").read_text())["providers"][LOCAL_MODEL_PROVIDER_ID]["models"]
    assert model["maxTokens"] == 4_096


def test_a_reasoning_model_is_declared_one_with_what_ollama_needs(tmp_path: Path) -> None:
    """The declaration alone would have pi send the instructions under a role Ollama drops."""
    profile = LocalModelProfile(context_window=32_768, is_reasoning=True, reasoning_off_effort="none")

    write_models_file(tmp_path, _OLLAMA, "qwen3:8b", profile)

    (model,) = json.loads((tmp_path / "models.json").read_text())["providers"][LOCAL_MODEL_PROVIDER_ID]["models"]
    assert model == {
        "id": "qwen3:8b",
        "contextWindow": 32_768,
        "maxTokens": 8_192,
        "reasoning": True,
        "compat": {"supportsDeveloperRole": False},
        "thinkingLevelMap": {"off": "none"},
    }


def test_a_reasoning_model_with_no_known_off_switch_names_none(tmp_path: Path) -> None:
    write_models_file(tmp_path, _OLLAMA, "m", LocalModelProfile(context_window=32_768, is_reasoning=True))

    (model,) = json.loads((tmp_path / "models.json").read_text())["providers"][LOCAL_MODEL_PROVIDER_ID]["models"]
    assert model["reasoning"] is True
    assert model["compat"] == {"supportsDeveloperRole": False}
    assert "thinkingLevelMap" not in model


def test_a_local_model_accounts_server_address_is_read_back_from_its_models_file(tmp_path: Path) -> None:
    write_models_file(
        tmp_path, "http://127.0.0.1:11435/v1", "qwen3:8b", LocalModelProfile(context_window=65_536, is_reasoning=False)
    )

    assert read_server_base_url(tmp_path) == "http://127.0.0.1:11435/v1"


def test_an_account_that_is_not_a_local_model_names_no_server(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "models.json").write_text(json.dumps({"providers": {"elsewhere": {"baseUrl": "http://x/v1"}}}))
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "models.json").write_text("{not json")

    assert read_server_base_url(empty) is None
    assert read_server_base_url(other) is None
    assert read_server_base_url(broken) is None


@pytest.mark.parametrize("status", [200, 401, 500])
def test_a_server_that_answers_anything_is_answering(status: int) -> None:
    """A refusal for want of a key is still a server a request would reach."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(status, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert is_server_answering(client, "http://127.0.0.1:11435/v1") is True
    assert seen == ["http://127.0.0.1:11435/v1/models"]


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("timed out"), httpx.RemoteProtocolError("dropped")],
)
def test_a_server_that_refuses_hangs_or_drops_the_connection_is_not_answering(error: httpx.HTTPError) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert is_server_answering(client, "http://127.0.0.1:11435/v1") is False
