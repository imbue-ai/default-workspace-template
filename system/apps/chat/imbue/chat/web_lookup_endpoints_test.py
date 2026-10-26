"""Tests for ``POST /api/chats/<chat_id>/web-lookup``: the route finds the chat's provider, account and model and
runs the web look-up that provider registers, so it names no provider itself."""

from pathlib import Path

import pytest
from flask.testing import FlaskClient

from imbue.chat.agent_manager import AgentManager
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.model import ModelChoice
from imbue.chat.harnesses.model import ModelIdentity
from imbue.chat.harnesses.model import ModelOption
from imbue.chat.harnesses.web_lookup import WebLookup
from imbue.chat.harnesses.web_lookup import WebLookupError
from imbue.chat.harnesses.web_lookup import WebLookupTimeoutError
from imbue.chat.models import AgentStateItem
from imbue.chat.server import create_application
from imbue.chat.testing import build_test_state
from imbue.chat.ws_broadcaster import WebSocketBroadcaster

_CHAT = "agent-00000000000000000000000000000001"
_BODY = {"system_prompt": "Find the person.", "prompt": "Find this person: Ada Lovelace, London."}


class _RecordingWebLookup(WebLookup):
    """Answers with a fixed text, or raises a fixed error, and keeps every call."""

    def __init__(self, answer: str = '{"candidates": []}', error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[tuple[Path, str, str, str | None]] = []

    def search(self, account_dir: Path, system_prompt: str, prompt: str, model: str | None) -> str:
        self.calls.append((account_dir, system_prompt, prompt, model))
        if self.error is not None:
            raise self.error
        return self.answer


def _choice(reported_id: str, matched_id: str | None) -> ModelChoice:
    identity = ModelIdentity(model_id=reported_id, effort=None, fast=False)
    matched = (
        None if matched_id is None else ModelOption(id=matched_id, label=matched_id, efforts=(), supports_fast=False)
    )
    return ModelChoice(identity=identity, matched=matched)


def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lookups: dict[HarnessType, WebLookup],
    harness: HarnessType = HarnessType.CLAUDE,
    model_choice: ModelChoice | None = None,
    is_agent_list_known: bool = True,
) -> FlaskClient:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    manager = AgentManager.build(WebSocketBroadcaster(), chat_files_root=tmp_path)
    if is_agent_list_known:
        manager.note_agent_list_known()
    state_dir = tmp_path / "agents" / _CHAT
    state_dir.mkdir(parents=True)
    (state_dir / "env").write_text(f"CLAUDE_CONFIG_DIR={tmp_path / 'account'}\n")
    manager._agents[_CHAT] = AgentStateItem(
        id=_CHAT,
        name="chat-1",
        state="RUNNING",
        labels={},
        work_dir="/code",
        harness=harness,
        model_choice=model_choice,
    )
    state = build_test_state(agent_manager=manager, web_lookup_builder=lookups.get)
    return create_application(state).test_client()


def test_the_search_runs_on_the_chats_account_and_the_alias_its_model_is_switched_by(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = _RecordingWebLookup(answer='{"candidates": [{"name": "Ada"}]}')
    client = _client(
        tmp_path, monkeypatch, {HarnessType.CLAUDE: lookup}, model_choice=_choice("claude-opus-5-5[1m]", "opus[1m]")
    )

    response = client.post(f"/api/chats/{_CHAT}/web-lookup", json=_BODY)

    assert response.status_code == 200, response.get_json()
    assert response.get_json() == {"answer": '{"candidates": [{"name": "Ada"}]}'}
    assert lookup.calls == [(tmp_path / "account", _BODY["system_prompt"], _BODY["prompt"], "opus[1m]")]


def test_a_model_outside_the_catalog_is_named_by_the_id_its_harness_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = _RecordingWebLookup()
    client = _client(
        tmp_path, monkeypatch, {HarnessType.CLAUDE: lookup}, model_choice=_choice("claude-opus-4-8", None)
    )

    client.post(f"/api/chats/{_CHAT}/web-lookup", json=_BODY)

    assert lookup.calls[0][3] == "claude-opus-4-8"


def test_a_chat_with_no_model_choice_yet_leaves_the_model_to_its_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = _RecordingWebLookup()
    client = _client(tmp_path, monkeypatch, {HarnessType.CLAUDE: lookup})

    client.post(f"/api/chats/{_CHAT}/web-lookup", json=_BODY)

    assert lookup.calls[0][3] is None


def test_each_provider_gets_its_own_look_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude, codex = _RecordingWebLookup(answer="from claude"), _RecordingWebLookup(answer="from codex")
    client = _client(
        tmp_path,
        monkeypatch,
        {HarnessType.CLAUDE: claude, HarnessType.CODEX: codex},
        harness=HarnessType.CODEX,
        model_choice=_choice("gpt-6-sol", "gpt-6-sol"),
    )

    response = client.post(f"/api/chats/{_CHAT}/web-lookup", json=_BODY)

    assert response.get_json() == {"answer": "from codex"}
    assert claude.calls == []
    assert codex.calls[0][3] == "gpt-6-sol"


def test_a_provider_with_no_web_look_up_is_refused_in_plain_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch, {HarnessType.CLAUDE: _RecordingWebLookup()}, harness=HarnessType.PI_CODING)

    response = client.post(f"/api/chats/{_CHAT}/web-lookup", json=_BODY)

    assert response.status_code == 501
    assert "pi-coding cannot run a web look-up yet" in response.get_json()["detail"]


def test_a_search_that_fails_answers_502_with_the_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = _RecordingWebLookup(error=WebLookupError("claude -p exited 1: Not logged in"))
    client = _client(tmp_path, monkeypatch, {HarnessType.CLAUDE: lookup})

    response = client.post(f"/api/chats/{_CHAT}/web-lookup", json=_BODY)

    assert response.status_code == 502
    assert "Not logged in" in response.get_json()["detail"]


def test_a_search_that_runs_out_of_time_answers_504(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = _RecordingWebLookup(error=WebLookupTimeoutError("claude -p did not answer within 300s"))
    client = _client(tmp_path, monkeypatch, {HarnessType.CLAUDE: lookup})

    assert client.post(f"/api/chats/{_CHAT}/web-lookup", json=_BODY).status_code == 504


def test_an_unknown_chat_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = _RecordingWebLookup()
    client = _client(tmp_path, monkeypatch, {HarnessType.CLAUDE: lookup})

    response = client.post("/api/chats/agent-ffffffffffffffffffffffffffffffff/web-lookup", json=_BODY)

    assert response.status_code == 404
    assert lookup.calls == []


def test_the_route_waits_for_the_agent_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = _RecordingWebLookup()
    client = _client(tmp_path, monkeypatch, {HarnessType.CLAUDE: lookup}, is_agent_list_known=False)

    assert client.post(f"/api/chats/{_CHAT}/web-lookup", json=_BODY).status_code == 503
    assert lookup.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"prompt": "x"},
        {"system_prompt": "x"},
        {"system_prompt": "", "prompt": "x"},
        {"system_prompt": "x", "prompt": "y" * 2001},
    ],
)
def test_a_malformed_body_is_refused_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict[str, str]
) -> None:
    lookup = _RecordingWebLookup()
    client = _client(tmp_path, monkeypatch, {HarnessType.CLAUDE: lookup})

    assert client.post(f"/api/chats/{_CHAT}/web-lookup", json=body).status_code == 400
    assert lookup.calls == []
