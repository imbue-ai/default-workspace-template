"""Unit tests for the copyable ``local_model`` helper.

The record is a file written as the set-up-local-model skill writes it, and the
account a folder laid out as the chat app's Local model sign-in leaves one, each
under a stand-in root. The model server is a real HTTP server on this machine's
loopback, answering the way an OpenAI-compatible server does, and so is the
gateway that stands in for the way to Imbue Studio.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from use_ai_integration_skill import local_model

_GATEWAY_VARIABLES = (
    "LATCHKEY_GATEWAY",
    "LATCHKEY_GATEWAY_PASSWORD",
    "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE",
    "MNGR_AGENT_ID",
)


class _LoopbackServer:
    """Answers every POST with ``answer`` (a status and a body), keeping what it was sent, and every GET with a
    model listing, counting them as ``probes``."""

    def __init__(self) -> None:
        self.answer: tuple[int, Any] = (
            200,
            {"choices": [{"message": {"content": "ok"}}]},
        )
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self.probes: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, payload: Any) -> None:
                raw = (
                    payload
                    if isinstance(payload, bytes)
                    else json.dumps(payload).encode()
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                server.probes.append(self.path)
                self._send(200, {"data": []})

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server.requests.append((self.path, dict(self.headers), body))
                self._send(*server.answer)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

        self._http = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._http.server_address[1])
        self.url = f"http://127.0.0.1:{self.port}"
        self.base_url = f"{self.url}/v1"
        # Closing waits out one poll, and every test closes its servers.
        self._thread = threading.Thread(
            target=self._http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._http.shutdown()
        self._http.server_close()
        self._thread.join()


@pytest.fixture
def model_server() -> Iterator[_LoopbackServer]:
    server = _LoopbackServer()
    yield server
    server.close()


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[_LoopbackServer]:
    """The workspace's way to Imbue Studio, wired into the environment as mngr wires an agent's."""
    server = _LoopbackServer()
    monkeypatch.setenv("LATCHKEY_GATEWAY", f"{server.url}/")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PASSWORD", "gateway-password")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "agent-jwt")
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-1")
    yield server
    server.close()


@pytest.fixture(autouse=True)
def _no_record_and_no_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A test starts in a workspace with nothing recorded and no way to Imbue Studio, wherever it runs."""
    monkeypatch.setenv(
        "MINDS_LOCAL_MODEL_ENDPOINT_FILE", str(tmp_path / "no-record.json")
    )
    for name in _GATEWAY_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def _record_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_url: str, **overrides: Any
) -> None:
    """Write the record the set-up-local-model skill leaves, and point the helper at it."""
    record = {
        "base_url": base_url,
        "model": "qwen3:8b-ctx16384",
        "base_model": "qwen3:8b",
        "context_window": 16_384,
        "reasoning_off_effort": "none",
        "updated_at": "2026-11-19T12:00:00+00:00",
        **overrides,
    }
    path = tmp_path / "state" / "local_model" / "endpoint.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record))
    monkeypatch.setenv("MINDS_LOCAL_MODEL_ENDPOINT_FILE", str(path))


def _connect_account(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    records: list[dict[str, Any]] | None,
    api_key: str = "local",
    account_id: str = "acct-local",
) -> Path:
    """Lay out one local-model account, among accounts of other lanes, and point the helper at the root."""
    folder = root / account_id
    folder.mkdir(parents=True)
    pi_models = [
        {"id": record["id"], "contextWindow": record["context_window"]}
        for record in records or [{"id": "phi-4", "context_window": 65_536}]
    ]
    (folder / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "local-model": {
                        "baseUrl": base_url,
                        "api": "openai-completions",
                        "models": pi_models,
                    }
                }
            }
        )
    )
    (folder / "auth.json").write_text(
        json.dumps({"local-model": {"type": "api_key", "key": api_key}})
    )
    if records is not None:
        (folder / "local_models.json").write_text(
            json.dumps({"is_context_managed": True, "records": records})
        )
    index = {
        "version": 2,
        "accounts": [
            {"id": "acct-claude", "lane": "anthropic"},
            {"id": account_id, "lane": "local-model"},
        ],
        "mru": "acct-claude",
        "default_account": None,
    }
    (root / "index.json").write_text(json.dumps(index))
    monkeypatch.setenv("MINDS_ACCOUNTS_ROOT", str(root))
    return folder


_QWEN_40K = {
    "id": "qwen3-40k:latest",
    "base_id": "qwen3:8b",
    "context_window": 40_960,
    "is_reasoning": True,
    "reasoning_off_effort": "none",
    "is_checked": True,
}
_LLAMA_64K_UNCHECKED = {
    "id": "llama3.3:latest-ctx65536",
    "base_id": "llama3.3:latest",
    "context_window": 65_536,
    "is_checked": False,
}
_LLAMA_128K = {
    "id": "llama3.3:latest-ctx131072",
    "base_id": "llama3.3:latest",
    "context_window": 131_072,
    "is_checked": True,
}


def test_the_endpoint_is_the_one_the_setup_recorded_with_no_chat_signed_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINDS_ACCOUNTS_ROOT", str(tmp_path / "no-accounts"))
    _record_model(tmp_path, monkeypatch, "http://127.0.0.1:11435/v1/")

    endpoint = local_model.read_local_model_endpoint()

    assert endpoint == local_model.LocalModelEndpoint(
        base_url="http://127.0.0.1:11435/v1",
        api_key="",
        model="qwen3:8b-ctx16384",
        context_window=16_384,
        reasoning_off_effort="none",
    )


def test_the_recorded_model_goes_by_either_of_its_names_and_before_a_chat_s_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_account(tmp_path / "accounts", monkeypatch, "http://chat/v1", [_QWEN_40K])
    _record_model(
        tmp_path, monkeypatch, "http://recorded/v1", reasoning_off_effort=None
    )

    endpoints = [
        local_model.read_local_model_endpoint(name)
        for name in (None, "qwen3:8b", "qwen3:8b-ctx16384")
    ]

    assert {
        (each.base_url, each.model, each.reasoning_off_effort) for each in endpoints
    } == {("http://recorded/v1", "qwen3:8b-ctx16384", None)}


def test_a_model_the_record_does_not_hold_comes_from_a_chat_s_connection_that_has_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_account(
        tmp_path / "accounts", monkeypatch, "http://chat/v1", [_QWEN_40K, _LLAMA_128K]
    )
    _record_model(tmp_path, monkeypatch, "http://recorded/v1")

    endpoint = local_model.read_local_model_endpoint("llama3.3:latest")

    assert (endpoint.base_url, endpoint.model) == (
        "http://chat/v1",
        "llama3.3:latest-ctx131072",
    )


def test_a_model_set_up_nowhere_is_refused_with_the_one_that_is_and_how_to_add_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINDS_ACCOUNTS_ROOT", str(tmp_path / "no-accounts"))
    _record_model(tmp_path, monkeypatch, "http://recorded/v1")

    with pytest.raises(
        local_model.LocalModelNotConnectedError,
        match=r"local model is qwen3:8b, and qwen3:14b is not set up\. .*set-up-local-model skill "
        r"\(its `start --model qwen3:14b`, then `verify --model qwen3:14b`\)",
    ):
        local_model.read_local_model_endpoint("qwen3:14b")


def test_a_record_that_names_no_address_or_no_model_is_as_good_as_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_account(tmp_path / "accounts", monkeypatch, "http://chat/v1", [_QWEN_40K])
    _record_model(tmp_path, monkeypatch, "http://recorded/v1", model="")

    assert local_model.read_local_model_endpoint().base_url == "http://chat/v1"


def test_the_endpoint_is_the_model_the_account_was_signed_in_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_account(
        tmp_path, monkeypatch, "http://127.0.0.1:11435/v1/", [_QWEN_40K, _LLAMA_128K]
    )

    endpoint = local_model.read_local_model_endpoint()

    assert endpoint == local_model.LocalModelEndpoint(
        base_url="http://127.0.0.1:11435/v1",
        api_key="",
        model="qwen3-40k:latest",
        context_window=40_960,
        reasoning_off_effort="none",
    )


def test_a_model_is_found_by_its_name_whichever_size_of_it_a_chat_has_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size nobody has used may not exist on the server yet, so it is passed over."""
    _connect_account(
        tmp_path,
        monkeypatch,
        "http://x/v1",
        [_QWEN_40K, _LLAMA_64K_UNCHECKED, _LLAMA_128K],
    )

    by_name = local_model.read_local_model_endpoint("llama3.3:latest")
    by_id = local_model.read_local_model_endpoint("qwen3-40k:latest")

    assert (by_name.model, by_name.context_window, by_name.reasoning_off_effort) == (
        "llama3.3:latest-ctx131072",
        131_072,
        None,
    )
    assert by_id.model == "qwen3-40k:latest"


def test_a_model_the_account_has_not_used_is_refused_with_the_ones_it_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_account(
        tmp_path, monkeypatch, "http://x/v1", [_QWEN_40K, _LLAMA_64K_UNCHECKED]
    )

    with pytest.raises(
        local_model.LocalModelNotConnectedError,
        match=r"no llama3.3:latest ready to use. It offers: qwen3:8b\. To call llama3.3:latest from code, "
        r"set it up with the set-up-local-model skill",
    ):
        local_model.read_local_model_endpoint("llama3.3:latest")


def test_an_account_from_before_its_models_were_kept_apart_runs_the_one_pi_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_account(tmp_path, monkeypatch, "http://x/v1", None, api_key="sk-server")

    endpoint = local_model.read_local_model_endpoint()

    assert (endpoint.model, endpoint.context_window, endpoint.api_key) == (
        "phi-4",
        65_536,
        "sk-server",
    )


def test_the_pinned_default_local_model_account_wins_over_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_account(
        tmp_path, monkeypatch, "http://first/v1", [_QWEN_40K], account_id="acct-first"
    )
    _connect_account(
        tmp_path,
        monkeypatch,
        "http://second/v1",
        [_LLAMA_128K],
        account_id="acct-second",
    )
    index = json.loads((tmp_path / "index.json").read_text())
    index["accounts"] = [
        {"id": "acct-first", "lane": "local-model"},
        {"id": "acct-second", "lane": "local-model"},
    ]
    (tmp_path / "index.json").write_text(json.dumps(index))
    assert local_model.read_local_model_endpoint().base_url == "http://first/v1"

    (tmp_path / "index.json").write_text(
        json.dumps({**index, "default_account": "acct-second"})
    )

    assert local_model.read_local_model_endpoint().base_url == "http://second/v1"


def test_a_workspace_with_no_local_model_says_how_to_set_one_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "index.json").write_text(
        json.dumps({"accounts": [{"id": "acct-claude", "lane": "anthropic"}]})
    )
    monkeypatch.setenv("MINDS_ACCOUNTS_ROOT", str(tmp_path))

    with pytest.raises(
        local_model.LocalModelNotConnectedError,
        match="No local model is set up for this workspace. Follow the set-up-local-model skill",
    ) as raised:
        local_model.local_model_completion("hi", system="Be brief.")

    assert "provider" not in str(raised.value)


def test_a_workspace_with_no_accounts_at_all_has_no_local_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINDS_ACCOUNTS_ROOT", str(tmp_path / "missing"))

    with pytest.raises(local_model.LocalModelNotConnectedError):
        local_model.read_local_model_endpoint()


def test_a_completion_is_one_system_and_one_user_message_with_the_reasoning_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_server: _LoopbackServer
) -> None:
    _connect_account(tmp_path, monkeypatch, model_server.base_url, [_QWEN_40K])
    model_server.answer = (
        200,
        {
            "choices": [
                {"message": {"role": "assistant", "content": "Bakery wins prize."}}
            ],
            "usage": {"prompt_tokens": 56, "completion_tokens": 10},
        },
    )

    result = local_model.local_model_completion(
        "Summarize: ...", system="You write terse summaries.", max_tokens=64
    )

    assert (result.text, result.model, result.input_tokens, result.output_tokens) == (
        "Bakery wins prize.",
        "qwen3-40k:latest",
        56,
        10,
    )
    ((path, headers, body),) = model_server.requests
    assert path == "/v1/chat/completions"
    assert "Authorization" not in headers
    assert body == {
        "model": "qwen3-40k:latest",
        "stream": False,
        "max_tokens": 64,
        "messages": [
            {"role": "system", "content": "You write terse summaries."},
            {"role": "user", "content": "Summarize: ..."},
        ],
        "reasoning_effort": "none",
    }


def test_a_completion_on_the_recorded_model_looks_for_the_server_before_it_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_server: _LoopbackServer
) -> None:
    _record_model(tmp_path, monkeypatch, model_server.base_url)

    result = local_model.local_model_completion("hi", system="Be brief.")

    assert (result.text, result.model) == ("ok", "qwen3:8b-ctx16384")
    assert model_server.probes == ["/v1/models"]
    ((path, _headers, body),) = model_server.requests
    assert (path, body["model"], body["reasoning_effort"]) == (
        "/v1/chat/completions",
        "qwen3:8b-ctx16384",
        "none",
    )


def test_a_completion_can_let_the_model_reason_ask_for_json_and_carries_the_server_s_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_server: _LoopbackServer
) -> None:
    _connect_account(
        tmp_path, monkeypatch, model_server.base_url, [_QWEN_40K], api_key="sk-server"
    )

    local_model.local_model_completion(
        "Classify.",
        system="Answer with JSON.",
        is_reasoning_wanted=True,
        response_format={"type": "json_object"},
    )

    ((_path, headers, body),) = model_server.requests
    assert headers["Authorization"] == "Bearer sk-server"
    assert "reasoning_effort" not in body
    assert body["response_format"] == {"type": "json_object"}


def test_a_model_that_does_not_reason_is_sent_no_reasoning_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_server: _LoopbackServer
) -> None:
    _connect_account(tmp_path, monkeypatch, model_server.base_url, [_LLAMA_128K])

    local_model.local_model_completion("hi", system="Be brief.")

    assert "reasoning_effort" not in model_server.requests[0][2]


def test_a_server_that_does_not_answer_is_unreachable_not_a_failure_of_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_server: _LoopbackServer
) -> None:
    _connect_account(tmp_path, monkeypatch, model_server.base_url, [_QWEN_40K])
    model_server.close()

    with pytest.raises(local_model.LocalModelUnreachableError, match="did not answer"):
        local_model.local_model_completion("hi", system="Be brief.", timeout_seconds=5)


def test_a_recorded_model_that_does_not_answer_is_connected_again_through_imbue_studio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_server: _LoopbackServer,
    gateway: _LoopbackServer,
) -> None:
    gone = _LoopbackServer()
    gone.close()
    _record_model(tmp_path, monkeypatch, gone.base_url)
    gateway.answer = (200, {"workspace_port": model_server.port})

    result = local_model.local_model_completion("hi", system="Be brief.")

    assert result.text == "ok"
    ((path, headers, body),) = gateway.requests
    assert path == "/minds-api-proxy/api/v1/agents/agent-1/local-model/tunnel"
    assert body == {"port": 11434}
    sent = {name.lower(): value for name, value in headers.items()}
    assert sent["x-latchkey-gateway-password"] == "gateway-password"
    assert sent["x-latchkey-gateway-permissions-override"] == "agent-jwt"
    assert model_server.probes == ["/v1/models"]
    assert [path for path, _, _ in model_server.requests] == ["/v1/chat/completions"]


@pytest.mark.parametrize(
    "answer",
    [
        (403, {"error": "may not", "code": "local_model_not_granted"}),
        (400, {"error": "'port' must be the port of a model server", "field": "port"}),
        (200, {"workspace_port": None}),
        (200, b"<html>not json</html>"),
    ],
)
def test_a_recorded_model_imbue_studio_does_not_bring_back_is_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway: _LoopbackServer,
    answer: tuple[int, Any],
) -> None:
    gone = _LoopbackServer()
    gone.close()
    _record_model(tmp_path, monkeypatch, gone.base_url)
    gateway.answer = answer

    with pytest.raises(
        local_model.LocalModelUnreachableError,
        match=f"The model server at {gone.base_url} did not answer",
    ):
        local_model.local_model_completion("hi", system="Be brief.")

    assert len(gateway.requests) == 1


def test_a_recorded_model_still_silent_after_being_connected_again_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gateway: _LoopbackServer
) -> None:
    gone = _LoopbackServer()
    gone.close()
    _record_model(tmp_path, monkeypatch, gone.base_url)
    gateway.answer = (200, {"workspace_port": gone.port})

    with pytest.raises(local_model.LocalModelUnreachableError):
        local_model.local_model_completion("hi", system="Be brief.")

    assert len(gateway.requests) == 1


def test_a_recorded_model_that_does_not_answer_is_unreachable_with_no_way_to_imbue_studio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = _LoopbackServer()
    gone.close()
    _record_model(tmp_path, monkeypatch, gone.base_url)

    with pytest.raises(
        local_model.LocalModelUnreachableError,
        match=f"The model server at {gone.base_url} did not answer",
    ):
        local_model.local_model_completion("hi", system="Be brief.")


def test_a_chat_s_connection_that_does_not_answer_is_left_as_the_chat_set_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_server: _LoopbackServer,
    gateway: _LoopbackServer,
) -> None:
    """Its tunnel may lead to another server than the one a setup's record is of."""
    gone = _LoopbackServer()
    gone.close()
    _connect_account(tmp_path, monkeypatch, gone.base_url, [_QWEN_40K])
    gateway.answer = (200, {"workspace_port": model_server.port})

    with pytest.raises(local_model.LocalModelUnreachableError):
        local_model.local_model_completion("hi", system="Be brief.")

    assert gateway.requests == []


@pytest.mark.parametrize(
    ("answer", "fragment"),
    [
        (
            (404, {"error": {"message": "model 'qwen3-40k:latest' not found"}}),
            "answered 404 for qwen3-40k:latest",
        ),
        ((200, b"<html>not json</html>"), "not JSON"),
        (
            (
                200,
                {"choices": [{"message": {"content": "", "reasoning": "thinking..."}}]},
            ),
            "answered with no text",
        ),
        ((200, {"choices": []}), "answered with no text"),
    ],
)
def test_an_answer_that_cannot_be_used_says_what_was_wrong(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_server: _LoopbackServer,
    answer: tuple[int, Any],
    fragment: str,
) -> None:
    _connect_account(tmp_path, monkeypatch, model_server.base_url, [_QWEN_40K])
    model_server.answer = answer

    with pytest.raises(local_model.LocalModelError, match=fragment) as raised:
        local_model.local_model_completion("hi", system="Be brief.")

    assert not isinstance(raised.value, local_model.LocalModelUnreachableError)
