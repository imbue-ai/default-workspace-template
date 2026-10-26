"""Chat naming through the real pi CLI, against a stand-in Anthropic API instead of a paid model."""

import json
import shutil
from pathlib import Path

import pytest

from imbue.chat.chat_naming import CHAT_NAMING_SYSTEM_PROMPT
from imbue.chat.chat_naming import chat_naming_prompt
from imbue.chat.harnesses.pi_coding.one_shot import PiOneShotCompletion
from imbue.chat.testing import StandInAnthropicApi
from imbue.chat.testing import make_pi_account_with_anthropic_key


@pytest.mark.skipif(shutil.which("pi") is None, reason="needs the pinned pi CLI on PATH")
def test_a_pi_account_is_asked_for_a_chat_name_on_its_haiku_with_nothing_but_the_naming_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_dir = make_pi_account_with_anthropic_key(tmp_path, api_key="sk-ant-5813")
    # The server's own key must not be the one the call is paid with.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "server-key-5813")
    # pi's bundled model catalog, not one refreshed over the network.
    monkeypatch.setenv("PI_OFFLINE", "1")

    with StandInAnthropicApi(reply_for=lambda _body: "Five days in Rome") as api:
        (account_dir / "models.json").write_text(json.dumps({"providers": {"anthropic": {"baseUrl": api.base_url}}}))
        answer = PiOneShotCompletion().complete(
            account_dir, CHAT_NAMING_SYSTEM_PROMPT, chat_naming_prompt("Plan Rome")
        )

    assert answer == "Five days in Rome"
    [request] = api.captured
    body = request["body"]
    assert request["x-api-key"] == "sk-ant-5813"
    assert "haiku" in body["model"]
    assert not body.get("tools")
    assert body.get("thinking", {"type": "disabled"})["type"] == "disabled"
    system_text = "".join(block["text"] for block in body["system"])
    assert system_text.startswith(CHAT_NAMING_SYSTEM_PROMPT)
    [message] = body["messages"]
    assert "".join(block["text"] for block in message["content"]) == chat_naming_prompt("Plan Rome")
