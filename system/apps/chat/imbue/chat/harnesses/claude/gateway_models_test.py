"""Unit tests for a gateway account's model list: reading the gateway, mapping its list, keeping it."""

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from imbue.chat.harnesses.claude.gateway_models import GATEWAY_MODELS_FILE_NAME
from imbue.chat.harnesses.claude.gateway_models import Gateway
from imbue.chat.harnesses.claude.gateway_models import cached_gateway_options
from imbue.chat.harnesses.claude.gateway_models import fetch_gateway_models_raw
from imbue.chat.harnesses.claude.gateway_models import gateway_accepts_key
from imbue.chat.harnesses.claude.gateway_models import gateway_options
from imbue.chat.harnesses.claude.gateway_models import gateway_options_for_account
from imbue.chat.harnesses.claude.gateway_models import read_gateway
from imbue.chat.harnesses.claude.model import CLAUDE_CATALOG
from imbue.chat.harnesses.claude.model import CLAUDE_EFFORTS
from imbue.chat.harnesses.claude.model import ClaudeModelResolver
from imbue.chat.harnesses.model import ModelIdentity
from imbue.chat.harnesses.model import match_option

# A trimmed copy of Concentrate's real `GET /v1/models` answer (pricing, capabilities and
# descriptions dropped): Claude models the catalog offers and hides, one it does not know, a
# model Concentrate marks unsupported on Claude Code, one it has not tagged, and two others.
_CONCENTRATE_MODELS = Path(__file__).parent / "testdata" / "concentrate_models.json"

_CATALOG = CLAUDE_CATALOG.options


def _concentrate_raw() -> dict[str, Any]:
    return json.loads(_CONCENTRATE_MODELS.read_text())


def _account(tmp_path: Path, env: dict[str, str]) -> Path:
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    (account_dir / "settings.json").write_text(json.dumps({"env": env}))
    return account_dir


def _gateway_account(tmp_path: Path) -> Path:
    return _account(tmp_path, {"ANTHROPIC_API_KEY": "sk-cn-xyz", "ANTHROPIC_BASE_URL": "https://api.concentrate.ai/"})


def _ids(options: tuple[Any, ...]) -> list[str]:
    return [option.id for option in options]


def _offered_ids(options: tuple[Any, ...]) -> list[str]:
    return [option.id for option in options if option.in_picker]


def _refuse(gateway: Gateway) -> dict[str, Any]:
    raise OSError("connection refused")


# -- read_gateway --


def test_a_gateway_account_names_its_address_and_key(tmp_path: Path) -> None:
    assert read_gateway(_gateway_account(tmp_path)) == Gateway(
        base_url="https://api.concentrate.ai", api_key="sk-cn-xyz"
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"env": {"ANTHROPIC_API_KEY": "sk-ant-xyz"}},
        {"env": {"ANTHROPIC_BASE_URL": "https://api.concentrate.ai"}},
        {"env": {"ANTHROPIC_API_KEY": "", "ANTHROPIC_BASE_URL": "https://api.concentrate.ai"}},
        {"env": "not a mapping"},
        {},
        ["not", "an", "object"],
    ],
)
def test_an_account_without_both_an_address_and_a_key_is_on_anthropic(tmp_path: Path, settings: Any) -> None:
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    assert read_gateway(tmp_path) is None


def test_an_account_with_no_or_unreadable_settings_is_on_anthropic(tmp_path: Path) -> None:
    assert read_gateway(tmp_path) is None
    (tmp_path / "settings.json").write_text("{not json")
    assert read_gateway(tmp_path) is None


# -- gateway_options --


def test_concentrates_list_maps_to_the_catalog_then_the_models_claude_code_can_run() -> None:
    options = gateway_options(_concentrate_raw(), _CATALOG, CLAUDE_EFFORTS)
    # The catalog's own offer comes first and unchanged, [1m] aliases and all; Claude ids the
    # catalog already matches (Opus 5.5, the hidden Sonnet 4.5) are not offered twice; the model
    # marked unsupported on Claude Code is dropped, and the untagged one is taken at its word.
    assert _offered_ids(options) == [
        *_offered_ids(_CATALOG),
        "gpt-5",
        "gemma-3-12b",
        "kimi-k2-6",
    ]
    by_id = {option.id: option for option in options}
    kimi = by_id["kimi-k2-6"]
    assert kimi.label == "Kimi K2.6"
    assert kimi.efforts == CLAUDE_EFFORTS
    assert kimi.supports_fast is False
    assert kimi.harness_reported_model_id == "kimi-k2-6"
    assert "gpt-oss-120b" not in by_id


def test_the_catalogs_hidden_models_still_name_a_live_read_on_a_gateway() -> None:
    """A chip on a model the picker does not offer (one chosen from claude's own /model) still
    resolves, as it does on an Anthropic account."""
    options = gateway_options(_concentrate_raw(), _CATALOG, CLAUDE_EFFORTS)
    assert _ids(options)[: len(_CATALOG)] == _ids(_CATALOG)
    hidden = match_option(ModelIdentity(model_id="claude-sonnet-4-5", effort=None, fast=False), options)
    assert hidden is not None and hidden.in_picker is False


def test_a_non_claude_model_from_the_gateway_matches_its_own_live_read() -> None:
    options = gateway_options(_concentrate_raw(), _CATALOG, CLAUDE_EFFORTS)
    matched = match_option(ModelIdentity(model_id="kimi-k2-6", effort="high", fast=False), options)
    assert matched is not None and matched.id == "kimi-k2-6"


def test_a_model_listed_twice_is_offered_once_and_a_bare_id_names_itself() -> None:
    raw = {"data": [{"id": "glm-5"}, {"slug": "glm-5", "name": "GLM 5"}, {"id": ""}, "not a model", {"name": "x"}]}
    options = gateway_options(raw, _CATALOG, CLAUDE_EFFORTS)
    extra = options[len(_CATALOG) :]
    assert [(option.id, option.label) for option in extra] == [("glm-5", "glm-5")]


def test_a_slug_and_name_stand_in_for_a_missing_id_and_display_name() -> None:
    options = gateway_options({"data": [{"slug": "glm-5", "name": "GLM 5"}]}, _CATALOG, CLAUDE_EFFORTS)
    assert [(option.id, option.label) for option in options[len(_CATALOG) :]] == [("glm-5", "GLM 5")]


@pytest.mark.parametrize("raw", [{}, {"data": None}, {"data": {"id": "glm-5"}}, {"error": "not found"}])
def test_an_answer_with_no_model_list_offers_the_catalog(raw: dict[str, Any]) -> None:
    assert gateway_options(raw, _CATALOG, CLAUDE_EFFORTS) == _CATALOG


def test_an_empty_model_list_offers_the_catalog() -> None:
    assert gateway_options({"data": []}, _CATALOG, CLAUDE_EFFORTS) == _CATALOG


# -- gateway_options_for_account --


def test_an_account_on_anthropic_asks_no_gateway(tmp_path: Path) -> None:
    account_dir = _account(tmp_path, {"ANTHROPIC_API_KEY": "sk-ant-xyz"})
    asked: list[Gateway] = []
    result = gateway_options_for_account(
        account_dir, _CATALOG, CLAUDE_EFFORTS, fetch=lambda gateway: asked.append(gateway) or {}
    )
    assert result is None
    assert asked == []
    assert not (account_dir / GATEWAY_MODELS_FILE_NAME).exists()


def test_a_fetched_list_is_kept_raw_beside_the_account(tmp_path: Path) -> None:
    account_dir = _gateway_account(tmp_path)
    raw = _concentrate_raw()
    asked: list[Gateway] = []
    options = gateway_options_for_account(
        account_dir, _CATALOG, CLAUDE_EFFORTS, fetch=lambda gateway: asked.append(gateway) or raw
    )
    assert asked == [Gateway(base_url="https://api.concentrate.ai", api_key="sk-cn-xyz")]
    assert options is not None and "kimi-k2-6" in _ids(options)
    assert json.loads((account_dir / GATEWAY_MODELS_FILE_NAME).read_text()) == raw


def test_a_failed_fetch_offers_the_list_kept_from_the_last_one(tmp_path: Path) -> None:
    account_dir = _gateway_account(tmp_path)
    raw = _concentrate_raw()
    fresh = gateway_options_for_account(account_dir, _CATALOG, CLAUDE_EFFORTS, fetch=lambda gateway: raw)
    offline = gateway_options_for_account(account_dir, _CATALOG, CLAUDE_EFFORTS, fetch=_refuse)
    assert offline == fresh


def test_a_failed_fetch_with_nothing_kept_offers_the_catalog_rather_than_nothing(tmp_path: Path) -> None:
    """An empty answer would leave the picker with no models at all."""
    account_dir = _gateway_account(tmp_path)
    assert gateway_options_for_account(account_dir, _CATALOG, CLAUDE_EFFORTS, fetch=_refuse) == _CATALOG

    def unparseable(gateway: Gateway) -> dict[str, Any]:
        raise ValueError("not JSON")

    assert gateway_options_for_account(account_dir, _CATALOG, CLAUDE_EFFORTS, fetch=unparseable) == _CATALOG


def test_a_list_that_cannot_be_kept_is_still_offered(tmp_path: Path) -> None:
    account_dir = _gateway_account(tmp_path)
    (account_dir / GATEWAY_MODELS_FILE_NAME).mkdir()
    options = gateway_options_for_account(
        account_dir, _CATALOG, CLAUDE_EFFORTS, fetch=lambda gateway: _concentrate_raw()
    )
    assert options is not None and "kimi-k2-6" in _ids(options)


def test_the_resolver_asks_the_account_it_is_handed(tmp_path: Path) -> None:
    """With no reachable gateway and nothing kept, the account-level ask still answers."""
    account_dir = _account(tmp_path, {"ANTHROPIC_API_KEY": "sk-cn-xyz", "ANTHROPIC_BASE_URL": "http://127.0.0.1:9"})
    assert ClaudeModelResolver.list_account_options(account_dir) == _CATALOG
    assert ClaudeModelResolver.list_account_options(tmp_path / "missing") is None


# -- cached_gateway_options --


def test_nothing_kept_is_no_options(tmp_path: Path) -> None:
    assert cached_gateway_options(tmp_path, _CATALOG, CLAUDE_EFFORTS) == ()
    (tmp_path / GATEWAY_MODELS_FILE_NAME).write_text("{truncated")
    assert cached_gateway_options(tmp_path, _CATALOG, CLAUDE_EFFORTS) == ()
    (tmp_path / GATEWAY_MODELS_FILE_NAME).write_text("[]")
    assert cached_gateway_options(tmp_path, _CATALOG, CLAUDE_EFFORTS) == ()


# -- fetch_gateway_models_raw, against a local stand-in for the gateway --


class _ModelsHandler(BaseHTTPRequestHandler):
    body: bytes = b"{}"
    status: int = 200
    seen: list[tuple[str, str | None, str | None]] = []
    posted: list[Any] = []

    def do_GET(self) -> None:
        type(self).seen.append((self.path, self.headers.get("x-api-key"), self.headers.get("anthropic-version")))
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def do_POST(self) -> None:
        type(self).posted.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.do_GET()

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def gateway_server() -> Iterator[tuple[str, type[_ModelsHandler]]]:
    handler = type("Handler", (_ModelsHandler,), {"seen": [], "posted": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", handler
    finally:
        server.shutdown()
        server.server_close()


def test_the_list_is_asked_for_with_the_accounts_key(gateway_server: tuple[str, type[_ModelsHandler]]) -> None:
    base_url, handler = gateway_server
    handler.body = _CONCENTRATE_MODELS.read_bytes()
    raw = fetch_gateway_models_raw(Gateway(base_url=base_url, api_key="sk-cn-xyz"))
    assert raw == _concentrate_raw()
    assert handler.seen == [("/v1/models", "sk-cn-xyz", "2023-06-01")]


def test_an_answer_that_is_not_an_object_is_a_failure(gateway_server: tuple[str, type[_ModelsHandler]]) -> None:
    base_url, handler = gateway_server
    handler.body = b"[]"
    with pytest.raises(ValueError):
        fetch_gateway_models_raw(Gateway(base_url=base_url, api_key="k"))
    handler.body = b"<html>"
    with pytest.raises(ValueError):
        fetch_gateway_models_raw(Gateway(base_url=base_url, api_key="k"))


def test_a_gateway_that_takes_the_key_accepts_it(gateway_server: tuple[str, type[_ModelsHandler]]) -> None:
    base_url, handler = gateway_server
    assert gateway_accepts_key(Gateway(base_url=base_url, api_key="sk-cn-xyz")) is True
    assert handler.seen == [("/v1/messages", "sk-cn-xyz", "2023-06-01")]


def test_a_gateway_that_refuses_the_key_rejects_it(gateway_server: tuple[str, type[_ModelsHandler]]) -> None:
    base_url, handler = gateway_server
    handler.status = 401
    assert gateway_accepts_key(Gateway(base_url=base_url, api_key="sk-wrong")) is False


def test_a_gateway_that_cannot_be_reached_gives_no_answer() -> None:
    assert gateway_accepts_key(Gateway(base_url="http://127.0.0.1:9", api_key="sk-cn-xyz")) is None


def test_the_key_check_asks_for_a_single_token_from_the_small_model(
    gateway_server: tuple[str, type[_ModelsHandler]],
) -> None:
    """Every sign-in pays for this request, so it must stay the cheapest one there is."""
    base_url, handler = gateway_server
    gateway_accepts_key(Gateway(base_url=base_url, api_key="sk-cn-xyz"))
    (posted,) = handler.posted
    assert posted["max_tokens"] == 1
    assert posted["model"] == "claude-haiku-4-5-20251001"


def test_a_forbidden_key_is_rejected_too(gateway_server: tuple[str, type[_ModelsHandler]]) -> None:
    base_url, handler = gateway_server
    handler.status = 403
    assert gateway_accepts_key(Gateway(base_url=base_url, api_key="sk-wrong")) is False


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
def test_a_gateway_error_that_is_not_about_the_key_gives_no_answer(
    gateway_server: tuple[str, type[_ModelsHandler]], status: int
) -> None:
    """A rate limit or an outage is not evidence against the key, so it must not throw it away."""
    base_url, handler = gateway_server
    handler.status = status
    assert gateway_accepts_key(Gateway(base_url=base_url, api_key="sk-cn-xyz")) is None


def test_a_garbled_answer_gives_no_answer() -> None:
    """http.client raises its own errors for a reply that is not HTTP, and they are not OSErrors."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]

        def answer_with_garbage() -> None:
            connection, _ = listener.accept()
            with connection:
                connection.recv(65536)
                connection.sendall(b"this is not http\r\n\r\n")

        thread = threading.Thread(target=answer_with_garbage, daemon=True)
        thread.start()
        assert gateway_accepts_key(Gateway(base_url=f"http://127.0.0.1:{port}", api_key="sk-cn-xyz")) is None
        thread.join(timeout=5)


def test_an_address_that_is_not_a_url_gives_no_answer() -> None:
    assert gateway_accepts_key(Gateway(base_url="api.concentrate.ai", api_key="sk-cn-xyz")) is None


def test_a_refused_key_is_a_failure(gateway_server: tuple[str, type[_ModelsHandler]]) -> None:
    base_url, handler = gateway_server
    handler.status = 401
    with pytest.raises(OSError):
        fetch_gateway_models_raw(Gateway(base_url=base_url, api_key="bad"))
