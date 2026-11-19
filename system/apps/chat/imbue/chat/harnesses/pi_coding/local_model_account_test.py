"""Tests for a local-model account's models: what a sign-in gives it, how it follows its server, and
what a switch to one of them does first.

The server is an `httpx.MockTransport` that keeps its models, so a copy made through it is
listed afterwards, as on Ollama.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from imbue.chat.harnesses.pi_coding.local_model import LOCAL_MODEL_PROVIDER_ID
from imbue.chat.harnesses.pi_coding.local_model import LocalModelError
from imbue.chat.harnesses.pi_coding.local_model import LocalModelProfile
from imbue.chat.harnesses.pi_coding.local_model_account import LocalModelAccount
from imbue.chat.harnesses.pi_coding.local_model_account import LocalModelRecord
from imbue.chat.harnesses.pi_coding.local_model_account import plan_local_model_sign_in
from imbue.chat.harnesses.pi_coding.local_model_account import prepare_model_switch
from imbue.chat.harnesses.pi_coding.local_model_account import read_account_models
from imbue.chat.harnesses.pi_coding.local_model_account import refresh_account_models
from imbue.chat.harnesses.pi_coding.local_model_account import with_checked_model
from imbue.chat.harnesses.pi_coding.local_model_account import write_account_models
from imbue.chat.harnesses.pi_coding.local_model_catalog import OLLAMA_CONTEXT_PARAMETER

_OLLAMA = "http://127.0.0.1:11434/v1"
_LM_STUDIO = "http://127.0.0.1:1234/v1"
_TOOL_CALL: dict[str, object] = {
    "role": "assistant",
    "content": "",
    "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
}
_TEXT_ONLY: dict[str, object] = {"role": "assistant", "content": "It is sunny in Paris."}


class _Ollama:
    """An Ollama that keeps its models: each is (limit, capabilities, parent, the context length it names)."""

    def __init__(self, reply: dict[str, object] = _TOOL_CALL) -> None:
        self.models: dict[str, tuple[int, tuple[str, ...], str, int | None]] = {}
        self.reply = reply
        self.asked: list[str] = []

    def add(
        self,
        name: str,
        limit: int = 131_072,
        capabilities: tuple[str, ...] = ("completion", "tools"),
        parent: str = "",
        named_context: int | None = None,
    ) -> None:
        self.models[name] = (limit, capabilities, parent, named_context)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: Any = json.loads(request.content) if request.content else {}
        if path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": name,
                            "details": {"parent_model": parent, "parameter_size": "8.2B", "context_length": limit},
                            "capabilities": list(capabilities),
                        }
                        for name, (limit, capabilities, parent, _) in self.models.items()
                    ]
                },
            )
        if path == "/api/show":
            named_context = self.models[body["model"]][3]
            parameters = f"{OLLAMA_CONTEXT_PARAMETER}    {named_context}" if named_context is not None else ""
            return httpx.Response(200, json={"parameters": parameters})
        if path == "/api/create":
            limit, capabilities, _, _ = self.models[body["from"]]
            self.models[body["model"]] = (
                limit,
                capabilities,
                body["from"],
                body["parameters"][OLLAMA_CONTEXT_PARAMETER],
            )
            return httpx.Response(200, json={"status": "success"})
        if path == "/v1/chat/completions":
            self.asked.append(body["model"])
            if body["model"] not in self.models:
                return httpx.Response(404, json={"error": {"message": f"model '{body['model']}' not found"}})
            return httpx.Response(200, json={"choices": [{"message": self.reply}]})
        if path == "/api/ps":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": name, "context_length": named_context}
                        for name, (_, _, _, named_context) in self.models.items()
                        if named_context is not None
                    ]
                },
            )
        return httpx.Response(404)


def _listing_server(model_ids: tuple[str, ...], reply: dict[str, object] = _TOOL_CALL) -> httpx.Client:
    """A server that is not Ollama: it lists its models and answers a chat, and says nothing else."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": model_id} for model_id in model_ids]})
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": reply}]})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handle))


def _unreachable() -> httpx.Client:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.Client(transport=httpx.MockTransport(refuse))


def _signed_in(account_dir: Path, client: httpx.Client, base_url: str, model_id: str, context_window: int) -> None:
    """An account dir as a sign-in on ``model_id`` leaves it, its model checked."""
    sign_in = plan_local_model_sign_in(client, base_url, "", model_id, context_window)
    profile = LocalModelProfile(context_window=sign_in.context_window, is_reasoning=False)
    write_account_models(account_dir, base_url, with_checked_model(sign_in.account, sign_in.model_id, profile))


def _pi_model_ids(account_dir: Path) -> list[str]:
    provider = json.loads((account_dir / "models.json").read_text())["providers"][LOCAL_MODEL_PROVIDER_ID]
    return [model["id"] for model in provider["models"]]


def test_a_sign_in_runs_the_copy_at_the_size_asked_for_and_makes_it_when_there_is_none() -> None:
    ollama = _Ollama()
    ollama.add("llama3.3:latest")

    sign_in = plan_local_model_sign_in(ollama.client(), _OLLAMA, "", "llama3.3:latest", 65_536)

    assert (sign_in.model_id, sign_in.context_window) == ("llama3.3:latest-ctx65536", 65_536)
    assert ollama.models["llama3.3:latest-ctx65536"] == (131_072, ("completion", "tools"), "llama3.3:latest", 65_536)


def test_a_sign_in_uses_the_copy_the_server_already_has_at_that_size() -> None:
    ollama = _Ollama()
    ollama.add("qwen3:8b", limit=40_960)
    ollama.add("qwen3-40k:latest", limit=40_960, parent="qwen3:8b", named_context=40_960)

    sign_in = plan_local_model_sign_in(ollama.client(), _OLLAMA, "", "qwen3:8b", 40_960)

    assert sign_in.model_id == "qwen3-40k:latest"
    assert set(ollama.models) == {"qwen3:8b", "qwen3-40k:latest"}


def test_a_size_past_the_model_s_limit_is_brought_down_to_the_largest_it_takes() -> None:
    ollama = _Ollama()
    ollama.add("qwen3:8b", limit=40_960)

    sign_in = plan_local_model_sign_in(ollama.client(), _OLLAMA, "", "qwen3:8b", 65_536)

    assert (sign_in.model_id, sign_in.context_window) == ("qwen3:8b-ctx40960", 40_960)


def test_the_account_is_given_every_model_a_chat_can_run_on_at_every_size_with_the_one_signed_in_on_first() -> None:
    ollama = _Ollama()
    ollama.add("qwen3:8b", limit=40_960, capabilities=("completion", "tools", "thinking"))
    ollama.add("llama3.3:latest", limit=131_072)
    ollama.add("qwen2.5:7b", limit=32_768)
    ollama.add("gemma3:4b", capabilities=("completion",))

    sign_in = plan_local_model_sign_in(ollama.client(), _OLLAMA, "", "llama3.3:latest", 65_536)

    assert sign_in.account.is_context_managed is True
    assert [(record.id, record.base_id, record.context_window) for record in sign_in.account.records] == [
        ("llama3.3:latest-ctx65536", "llama3.3:latest", 65_536),
        ("qwen3:8b-ctx40960", "qwen3:8b", 40_960),
        ("llama3.3:latest-ctx40960", "llama3.3:latest", 40_960),
        ("llama3.3:latest-ctx131072", "llama3.3:latest", 131_072),
    ]
    # Only the size signed in on was made: the rest are made as a chat asks for them.
    assert [name for name in ollama.models if "-ctx" in name] == ["llama3.3:latest-ctx65536"]
    qwen = sign_in.account.records[1]
    assert (qwen.is_reasoning, qwen.reasoning_off_effort) == (True, "none")
    assert all(not record.is_checked for record in sign_in.account.records)


@pytest.mark.parametrize(
    ("model_id", "context_window", "fragment"),
    [
        ("qwen2.5:7b", 65_536, "qwen2.5:7b cannot run a chat here. It takes a context of 32,768 tokens at most"),
        ("gemma3:4b", 65_536, "gemma3:4b cannot run a chat here. It does not call tools"),
        ("missing:latest", 65_536, "does not list missing:latest"),
        ("qwen3:8b", 20_000, "at least 40,960 tokens"),
        ("  ", 65_536, "Pick a model."),
    ],
)
def test_a_sign_in_on_a_model_that_cannot_run_a_chat_is_refused_with_why(
    model_id: str, context_window: int, fragment: str
) -> None:
    ollama = _Ollama()
    ollama.add("qwen3:8b", limit=40_960)
    ollama.add("qwen2.5:7b", limit=32_768)
    ollama.add("gemma3:4b", capabilities=("completion",))

    with pytest.raises(LocalModelError, match=fragment):
        plan_local_model_sign_in(ollama.client(), _OLLAMA, "", model_id, context_window)
    assert not any("-ctx" in name for name in ollama.models)


def test_a_server_that_says_nothing_of_its_models_gives_the_account_each_as_listed_at_the_size_named() -> None:
    client = _listing_server(("phi-4", "mistral-small", "qwen3-coder"))

    sign_in = plan_local_model_sign_in(client, _LM_STUDIO, "", "mistral-small", 65_536)

    assert (sign_in.model_id, sign_in.context_window) == ("mistral-small", 65_536)
    assert sign_in.account.is_context_managed is False
    assert [(record.id, record.base_id, record.context_window) for record in sign_in.account.records] == [
        ("mistral-small", "mistral-small", 65_536),
        ("phi-4", "phi-4", 65_536),
        ("qwen3-coder", "qwen3-coder", 65_536),
    ]


def test_the_checked_model_is_told_to_pi_as_the_check_found_it() -> None:
    account = LocalModelAccount(
        is_context_managed=False,
        records=(
            LocalModelRecord(id="a", base_id="a", context_window=65_536),
            LocalModelRecord(id="b", base_id="b", context_window=65_536),
        ),
    )

    checked = with_checked_model(account, "a", LocalModelProfile(context_window=49_152, is_reasoning=True))

    assert checked.records == (
        LocalModelRecord(id="a", base_id="a", context_window=49_152, is_reasoning=True, is_checked=True),
        LocalModelRecord(id="b", base_id="b", context_window=65_536),
    )


def test_the_account_s_models_are_written_for_pi_and_read_back(tmp_path: Path) -> None:
    ollama = _Ollama()
    ollama.add("qwen3:8b", limit=40_960)
    ollama.add("llama3.3:latest")

    _signed_in(tmp_path, ollama.client(), _OLLAMA, "qwen3:8b", 40_960)

    assert _pi_model_ids(tmp_path) == [
        "qwen3:8b-ctx40960",
        "llama3.3:latest-ctx40960",
        "llama3.3:latest-ctx65536",
        "llama3.3:latest-ctx131072",
    ]
    account = read_account_models(tmp_path)
    assert account is not None
    assert [record.is_checked for record in account.records] == [True, False, False, False]


def test_an_agent_s_config_dir_leads_to_its_account_s_models(tmp_path: Path) -> None:
    """An agent holds a link to its account's `models.json`, and the account's other file sits beside that."""
    account_dir, config_dir = tmp_path / "account", tmp_path / "agent" / "plugin" / "pi_coding"
    account_dir.mkdir()
    config_dir.mkdir(parents=True)
    _signed_in(account_dir, _listing_server(("phi-4",)), _LM_STUDIO, "phi-4", 65_536)
    (config_dir / "models.json").symlink_to(account_dir / "models.json")

    account = read_account_models(config_dir)

    assert account is not None
    assert [record.id for record in account.records] == ["phi-4"]


def test_an_account_that_keeps_no_models_of_its_own_has_none_to_read(tmp_path: Path) -> None:
    assert read_account_models(tmp_path) is None
    assert refresh_account_models(_unreachable(), tmp_path) is None


def test_a_file_that_cannot_be_read_as_the_account_s_models_is_no_models(tmp_path: Path) -> None:
    (tmp_path / "local_models.json").write_text("{not json")

    assert read_account_models(tmp_path) is None


def test_a_model_pulled_since_shows_up_and_one_removed_goes(tmp_path: Path) -> None:
    ollama = _Ollama()
    ollama.add("qwen3:8b", limit=40_960)
    ollama.add("llama3.3:latest", limit=65_536)
    _signed_in(tmp_path, ollama.client(), _OLLAMA, "qwen3:8b", 40_960)
    del ollama.models["llama3.3:latest"]
    ollama.add("mistral-small:latest", limit=40_960)

    refreshed = refresh_account_models(ollama.client(), tmp_path)

    assert refreshed is not None
    assert [(record.id, record.is_checked) for record in refreshed.records] == [
        ("qwen3:8b-ctx40960", True),
        ("mistral-small:latest-ctx40960", False),
    ]
    assert _pi_model_ids(tmp_path) == ["qwen3:8b-ctx40960", "mistral-small:latest-ctx40960"]


def test_a_refresh_that_changes_nothing_writes_nothing(tmp_path: Path) -> None:
    ollama = _Ollama()
    ollama.add("qwen3:8b", limit=40_960)
    _signed_in(tmp_path, ollama.client(), _OLLAMA, "qwen3:8b", 40_960)
    written_at = (tmp_path / "models.json").stat().st_mtime_ns

    refresh_account_models(ollama.client(), tmp_path)

    assert (tmp_path / "models.json").stat().st_mtime_ns == written_at


def test_a_server_that_does_not_answer_leaves_the_account_as_it_is(tmp_path: Path) -> None:
    ollama = _Ollama()
    ollama.add("qwen3:8b", limit=40_960)
    _signed_in(tmp_path, ollama.client(), _OLLAMA, "qwen3:8b", 40_960)
    before = read_account_models(tmp_path)

    assert refresh_account_models(_unreachable(), tmp_path) == before
    assert _pi_model_ids(tmp_path) == ["qwen3:8b-ctx40960"]


def test_a_server_that_only_lists_its_models_is_followed_too(tmp_path: Path) -> None:
    _signed_in(tmp_path, _listing_server(("phi-4",)), _LM_STUDIO, "phi-4", 49_152)

    refreshed = refresh_account_models(_listing_server(("phi-4", "mistral-small")), tmp_path)

    assert refreshed is not None
    assert [(record.id, record.context_window) for record in refreshed.records] == [
        ("phi-4", 49_152),
        ("mistral-small", 49_152),
    ]


def test_a_switch_to_a_size_nobody_asked_for_yet_makes_the_copy_and_checks_it(tmp_path: Path) -> None:
    ollama = _Ollama()
    ollama.add("llama3.3:latest")
    _signed_in(tmp_path, ollama.client(), _OLLAMA, "llama3.3:latest", 40_960)

    prepare_model_switch(ollama.client(), tmp_path, "llama3.3:latest-ctx131072")

    assert ollama.models["llama3.3:latest-ctx131072"][2:] == ("llama3.3:latest", 131_072)
    assert ollama.asked == ["llama3.3:latest-ctx131072"]
    account = read_account_models(tmp_path)
    assert account is not None
    assert {record.id: record.is_checked for record in account.records} == {
        "llama3.3:latest-ctx40960": True,
        "llama3.3:latest-ctx65536": False,
        "llama3.3:latest-ctx131072": True,
    }


def test_a_switch_to_a_model_already_checked_asks_the_server_nothing(tmp_path: Path) -> None:
    ollama = _Ollama()
    ollama.add("qwen3:8b", limit=40_960)
    _signed_in(tmp_path, ollama.client(), _OLLAMA, "qwen3:8b", 40_960)

    prepare_model_switch(_unreachable(), tmp_path, "qwen3:8b-ctx40960")


def test_a_switch_to_a_model_that_prints_its_tool_calls_is_refused_and_leaves_it_unchecked(tmp_path: Path) -> None:
    ollama = _Ollama()
    ollama.add("qwen3:8b", limit=40_960)
    ollama.add("qwen2.5-coder:32b", limit=65_536)
    _signed_in(tmp_path, ollama.client(), _OLLAMA, "qwen3:8b", 40_960)
    ollama.reply = _TEXT_ONLY

    with pytest.raises(LocalModelError, match="answered without calling a tool"):
        prepare_model_switch(ollama.client(), tmp_path, "qwen2.5-coder:32b-ctx65536")

    account = read_account_models(tmp_path)
    assert account is not None
    assert {record.id: record.is_checked for record in account.records}["qwen2.5-coder:32b-ctx65536"] is False


def test_a_switch_while_the_server_does_not_answer_says_so(tmp_path: Path) -> None:
    ollama = _Ollama()
    ollama.add("llama3.3:latest")
    _signed_in(tmp_path, ollama.client(), _OLLAMA, "llama3.3:latest", 40_960)

    with pytest.raises(LocalModelError, match="did not answer"):
        prepare_model_switch(_unreachable(), tmp_path, "llama3.3:latest-ctx65536")


def test_a_switch_on_a_server_that_only_lists_its_models_checks_the_model_as_named(tmp_path: Path) -> None:
    client = _listing_server(("phi-4", "mistral-small"))
    _signed_in(tmp_path, client, _LM_STUDIO, "phi-4", 65_536)

    prepare_model_switch(client, tmp_path, "mistral-small")

    account = read_account_models(tmp_path)
    assert account is not None
    assert [(record.id, record.is_checked) for record in account.records] == [("phi-4", True), ("mistral-small", True)]


def test_a_switch_to_a_model_the_account_does_not_name_is_left_to_pi(tmp_path: Path) -> None:
    _signed_in(tmp_path, _listing_server(("phi-4",)), _LM_STUDIO, "phi-4", 65_536)

    prepare_model_switch(_unreachable(), tmp_path, "some-other-model")
    prepare_model_switch(_unreachable(), tmp_path / "no-such-account", "phi-4")


def test_the_server_s_key_is_sent_when_the_account_holds_one(tmp_path: Path) -> None:
    seen: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "phi-4"}, {"id": "mistral-small"}]})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    _signed_in(tmp_path, client, _LM_STUDIO, "phi-4", 65_536)
    (tmp_path / "auth.json").write_text(json.dumps({LOCAL_MODEL_PROVIDER_ID: {"type": "api_key", "key": "sk-secret"}}))
    seen.clear()

    refresh_account_models(client, tmp_path)

    assert "Bearer sk-secret" in seen
